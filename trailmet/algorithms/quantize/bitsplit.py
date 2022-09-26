

import os
import torch
import copy
import torch.nn as nn
from trailmet.utils import seed_everything
from trailmet.algorithms.quantize.qmodel_bitsplit import QuantModel, Quantizer
from trailmet.algorithms.quantize.quantize import BaseQuantization


class BitSplit(BaseQuantization):
    def __init__(self, model: nn.Module, dataloaders, **kwargs):
        super(BitSplit, self).__init__(**kwargs)
        self.model = model
        self.train_loader = dataloaders['train']
        self.val_loader = dataloaders['val']
        self.kwargs = kwargs
        self.w_bits = self.kwargs.get('W_BITS', 8)
        self.a_bits = self.kwargs.get('A_BITS', 8)
        self.gpu_id = self.kwargs.get('GPU_ID', 0)
        self.seed = self.kwargs.get('SEED', 42)
        self.device = torch.device('cuda:{}'.format(self.gpu_id))
        torch.cuda.set_device(self.gpu_id)
        seed_everything(self.seed)
        self.arch = self.kwargs.get('ARCH', "ResNet50")
        self.prefix = self.arch + '/A'+str(self.a_bits)+'W'+str(self.w_bits)
        if not os.path.exists(self.prefix):
            os.makedirs(self.prefix)
        self.scales = False
        self.load_scales = self.kwargs.get('LOAD_SCALES', False)

        
    def compress_model(self):
        self.model.to(self.device)
        self.qmodel = copy.deepcopy(self.model)
        QuantModel(self.qmodel)

        self.act_quant_modules = []
        for m in self.qmodel.modules():
            if isinstance(m, Quantizer):
                m.set_bitwidth(self.a_bits)
                self.act_quant_modules.append(m)
        self.act_quant_modules[-1].set_bitwidth(8)

        #############################
        #### Weight Quantization ####
        #############################
        conv = self.model.conv1
        q_conv = self.qmodel.conv1
        conduct_ofwa(self.train_loader, self.model, self.qmodel, conv, q_conv, None, 8,
                    prefix=self.prefix+'/conv1', device=self.device, ec=False)
        #### quantize 4 blocks
        for layer_idx in range(1, 5):
            current_layer_pretrained = eval('self.model.layer{}'.format(layer_idx))
            current_layer_quan = eval('self.qmodel.layer{}'.format(layer_idx))
            for block_idx in range(len(current_layer_pretrained)):
                current_block_pretrained = current_layer_pretrained[block_idx]
                current_block_quan = current_layer_quan[block_idx]
                pkl_path = self.prefix+'/layer'+str(layer_idx)+'_block'+str(block_idx)
                # conv1
                conv = current_block_pretrained.conv1
                conv_quan = current_block_quan.conv1
                q_module = current_block_quan.quant1
                conduct_ofwa(self.train_loader, self.model, self.qmodel, conv, conv_quan, q_module, 
                            self.w_bits, prefix=pkl_path+'_conv1', device=self.device, ec=False)
                # conv2
                conv = current_block_pretrained.conv2
                conv_quan = current_block_quan.conv2
                q_module = current_block_quan.quant2
                conduct_ofwa(self.train_loader, self.model, self.qmodel, conv, conv_quan, q_module, 
                            self.w_bits, prefix=pkl_path+'_conv2', device=self.device, ec=False)
                # downsample
                if current_block_pretrained.downsample is not None:
                    conv = current_block_pretrained.downsample[0]
                    conv_quan = current_block_quan.downsample[0]
                    conduct_ofwa(self.train_loader, self.model, self.qmodel, conv, conv_quan, None, 
                                self.w_bits, prefix=pkl_path+'_downsample', device=self.device, ec=False)
        ## quantize last fc
        conv = self.model.fc
        conv_quan = self.qmodel.fc[1]
        q_module = self.qmodel.quant
        conduct_ofwa(self.train_loader, self.model, self.qmodel, conv, conv_quan, q_module, 8, 
                    prefix=self.prefix+'/fc', device=self.device, ec=False)
        
        ##################################
        #### Load Weight Quantization ####
        ##################################
        conv = self.model.conv1
        conv_quan = self.qmodel.conv1
        load_ofwa(conv, conv_quan, None, 8, prefix=self.prefix+'/conv1')
        for layer_idx in range(1, 5):
            current_layer_pretrained = eval('self.model.layer{}'.format(layer_idx))
            current_layer_quan = eval('self.qmodel.layer{}'.format(layer_idx))
            for block_idx in range(len(current_layer_pretrained)):
                current_block_pretrained = current_layer_pretrained[block_idx]
                current_block_quan = current_layer_quan[block_idx]
                # conv1
                conv = current_block_pretrained.conv1
                conv_quan = current_block_quan.conv1
                q_module = current_block_quan.quant1
                load_ofwa(conv, conv_quan, q_module, self.w_bits, prefix=self.prefix+'/layer'+str(layer_idx)+'_block'+str(block_idx)+'_conv1')
                # conv2
                conv = current_block_pretrained.conv2
                conv_quan = current_block_quan.conv2
                q_module = current_block_quan.quant2
                load_ofwa(conv, conv_quan, q_module, self.w_bits, prefix=self.prefix+'/layer'+str(layer_idx)+'_block'+str(block_idx)+'_conv2')
                # downsample
                if current_block_pretrained.downsample is not None:
                    conv = current_block_pretrained.downsample[0]
                    conv_quan = current_block_quan.downsample[0]
                    load_ofwa(conv, conv_quan, None, self.w_bits, prefix=self.prefix+'/layer'+str(layer_idx)+'_block'+str(block_idx)+'_downsample')
        conv = self.model.fc
        conv_quan = self.qmodel.fc[1]
        q_module = self.qmodel.quant
        load_ofwa(conv, conv_quan, q_module, 8, prefix=self.prefix+'/fc')
        
        #################################
        #### Activation Quantization ####
        #################################
        print("quantizing ('{}-bit')...".format(self.a_bits))
        # update(train_loader, model_quan, criterion, args, 200)
        if self.scales:
            scales = np.load(self.scales)
            # enable feature map quantization
            for index, q_module in enumerate(self.act_quant_modules):
                q_module.set_scale(scales[index])
        else:
            quantize(self.train_loader, self.qmodel, self.a_bits, self.act_quant_modules,
                    device=self.device, prefix=self.prefix)

        # print('update ...')
        # update(train_loader, model_quan, criterion, args, 200)
        # print('validate quantization...')
        # validate(val_loader, model_quan, criterion, args)

        save_state_dict(self.qmodel.state_dict(), self.prefix, filename='state_dict.pth')



import numpy as np
import random
import pickle
from collections import OrderedDict
from trailmet.algorithms.quantize.bitsplit_dump import ofwa, ofwa_rr

global feat, prev_feat, conv_feat
def hook(module, input, output):
    global feat
    feat = output.data.cpu().numpy()
def current_input_hook(module, inputdata, outputdata):
    global prev_feat
    prev_feat = inputdata[0].data#.cpu()#.numpy()
def conv_hook(module, inputdata, outputdata):
    global conv_feat
    conv_feat = outputdata.data#.cpu()#.numpy()


def conduct_ofwa(train_loader, model_pretrained, model_quan, conv, conv_quan, 
                q_module, bitwidth, device, prefix=None, ec=False):
    # for fc
    if not hasattr(conv, 'kernel_size'):
        W = conv.weight.data#.cpu()
        W_shape = W.shape
        B_sav, B, alpha = ofwa(W.cpu().numpy(), bitwidth)
        with open(prefix + '_fwa.pkl', 'wb') as f:
            pickle.dump({'B': B, 'alpha': alpha}, f, pickle.HIGHEST_PROTOCOL)
        if ec:
            W_r = np.multiply(B, np.expand_dims(alpha, 1)).reshape(W_shape)
            conv_quan.weight.data.copy_(torch.from_numpy(W_r))
        return

    # conv parameters
    kernel_h, kernel_w = conv.kernel_size
    pad_h, pad_w = conv.padding
    stride_h, stride_w = conv.stride

    handle_prev = conv_quan.register_forward_hook(current_input_hook)
    handle_conv = conv.register_forward_hook(conv_hook)

    batch_iterator = iter(train_loader)

    # weights and bias
    W = conv.weight.data#.cpu()
    if conv.bias is None:
        bias = torch.zeros(W.shape[0]).to(conv.weight.device)
    else:
        bias = conv.bias.data#.cpu()
    print(W.shape)

    # feat extract
    n_batches = 30
    per_batch = 400
    input, target = next(batch_iterator)
    input_pretrained = input.cuda(device=device, non_blocking=True)
    input_quan = input.cuda(device=device, non_blocking=True)
    model_pretrained(input_pretrained)
    model_quan(input_quan)
    # print(prev_feat.shape)
    # print(conv_feat.shape)
    [prev_feat_n, prev_feat_c, prev_feat_h, prev_feat_w] = prev_feat.shape
    [conv_feat_n, conv_feat_c, conv_feat_h, conv_feat_w] = conv_feat.shape

    X = torch.zeros(n_batches*per_batch, prev_feat_c, kernel_h, kernel_w).to(device)
    Y = torch.zeros(n_batches*per_batch, conv_feat_c).to(device)
    print(X.shape)
    print(Y.shape)

    for batch_idx in range(0, n_batches):
        input, target = next(batch_iterator)
        input_pretrained = input.cuda(device=device, non_blocking=True)
        model_pretrained(input_pretrained)
        input_quan = input.cuda(device=device, non_blocking=True)
        model_quan(input_quan)
    
        prev_feat_pad = torch.zeros(prev_feat_n, prev_feat_c, prev_feat_h+2*pad_h, prev_feat_w+2*pad_w).to(device)
        prev_feat_pad[:, :, pad_h:pad_h+prev_feat_h, pad_w:pad_w+prev_feat_w] = prev_feat
        prev_feat_pad = prev_feat_pad.unfold(2, kernel_h, stride_h).unfold(3, kernel_w, stride_w).permute(0,2,3,1,4,5)
        [feat_pad_n, feat_pad_h, feat_pad_w, feat_pad_c, feat_pad_hh, feat_pad_ww] = prev_feat_pad.shape
        assert(feat_pad_hh==kernel_h)
        assert(feat_pad_ww==kernel_w)
        # prev_feat_pad = prev_feat_pad.reshape(feat_pad_n*feat_pad_h*feat_pad_w, -1)
        prev_feat_pad = prev_feat_pad.reshape(feat_pad_n*feat_pad_h*feat_pad_w, feat_pad_c, kernel_h, kernel_w)
        rand_index = list(range(prev_feat_pad.shape[0]))
        random.shuffle(rand_index)
        rand_index = rand_index[0:per_batch]
        X[per_batch*batch_idx:per_batch*(batch_idx+1),:] = prev_feat_pad[rand_index, :]
        conv_feat_tmp = conv_feat.permute(0,2,3,1).reshape(-1, conv_feat_c) - bias
        Y[per_batch*batch_idx:per_batch*(batch_idx+1),:] = conv_feat_tmp[rand_index, :]
    
    handle_prev.remove()
    handle_conv.remove()
    
    ## ofwa init
    W_shape = W.shape
    X = X.cpu().numpy()
    Y = Y.cpu().numpy()
    W = W.reshape(W_shape[0], -1)
    B_sav, B, alpha = ofwa(W.cpu().numpy(), bitwidth)
    B, alpha = ofwa_rr(X, Y, B_sav, alpha, bitwidth, max_epoch=100)
    with open(prefix + '_rr_b30x400_e100.pkl', 'wb') as f:
        pickle.dump({'B': B, 'alpha': alpha}, f, pickle.HIGHEST_PROTOCOL)


def load_ofwa(conv, conv_quan, q_module, bitwidth, prefix=None):
    # for fc
    if not hasattr(conv, 'kernel_size'):
        W = conv.weight.data#.cpu()
        W_shape = W.shape
        with open(prefix + '_fwa.pkl', 'rb') as f:
            B_alpha = pickle.load(f)
            B = B_alpha['B']
            alpha = B_alpha['alpha']
        W_r = np.multiply(B, np.expand_dims(alpha, 1)).reshape(W_shape)
        conv_quan.weight.data.copy_(torch.from_numpy(W_r))
        return

    # weights and bias
    W = conv.weight.data#.cpu()
    W_shape = W.shape

    with open(prefix + '_rr_b30x400_e100.pkl', 'rb') as f:
        B_alpha = pickle.load(f)
        B = B_alpha['B']
        alpha = B_alpha['alpha']
    W_r = np.multiply(B, np.expand_dims(alpha, 1)).reshape(W_shape)
    conv_quan.weight.data.copy_(torch.from_numpy(W_r))


def save_state_dict(state_dict, path, filename='state_dict.pth'):
    saved_path = os.path.join(path, filename)
    new_state_dict = OrderedDict()
    for key in state_dict.keys():
        if '.module.' in key:
            new_state_dict[key.replace('.module.', '.')] = state_dict[key].cpu()
        else:
            new_state_dict[key] = state_dict[key].cpu()
    torch.save(new_state_dict, saved_path)


def quantize(trainloader, model, a_bits, act_quant_modules, device, prefix):
    
    def get_safelen(x):
        x = x / 10
        y = 1
        while(x>=10):
            x = x / 10
            y = y * 10
        return int(y)

    act_sta_len = 3000000
    feat_buf = np.zeros(act_sta_len)
    scales = np.zeros(len(act_quant_modules))

    with torch.no_grad():
        for index, q_module in enumerate(act_quant_modules):
            # batch_iterator = iter(torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                                                            #  num_workers=args.workers, pin_memory=True))
            batch_iterator = iter(trainloader)
            images, targets = next(batch_iterator)
            images = images.cuda()
            targets = targets.cuda()

            #### ADD HANDLE ####
            handle = q_module.register_forward_hook(hook)
            model(images)

            #global feat
            feat_len = feat.size
            per_batch = min(get_safelen(feat_len), 100000)
            # n_batches = int(act_sta_len / per_batch)
            n_batches = 4

            failed = True
            while(failed):
                failed = False
                print('Extracting features for ', n_batches, ' batches...')
                for batch_idx in range(0, n_batches):
                    images, targets = next(batch_iterator)
                    images = images.cuda(device=device, non_blocking=True)
                    # forward
                    model(images)

                    #global feat
                    if q_module.signed:
                        feat_tmp = np.abs(feat).reshape(-1)
                    else:
                        feat_tmp = feat[feat>0].reshape(-1)
                        if feat_tmp.size < per_batch:
                            per_batch = int(per_batch / 10)
                            n_batches = int(n_batches * 10)
                            failed = True
                            break
                    np.random.shuffle(feat_tmp)
                    feat_buf[batch_idx*per_batch:(batch_idx+1)*per_batch] = feat_tmp[0:per_batch]

                if(not failed):
                    print('Init quantization... ')
                    scales[index] = q_module.init_quantization(feat_buf)
                    print(scales[index])
                    np.save(os.path.join(prefix, 'act_' + str(a_bits) + '_scales.npy'), scales)
            #### REMOVE HANDLE ####
            handle.remove()

    np.save(os.path.join(prefix, 'act_' + str(a_bits) + '_scales.npy'), scales)
    # enable feature map quantization
    for index, q_module in enumerate(act_quant_modules):
        q_module.set_scale(scales[index])

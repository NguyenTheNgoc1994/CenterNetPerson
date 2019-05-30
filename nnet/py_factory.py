import os
import pdb
import torch
import importlib
import torch.nn as nn

from config import system_configs
from models.py_utils.data_parallel import DataParallel

torch.manual_seed(317)

class Network(nn.Module):
    def __init__(self, model, loss):
        super(Network, self).__init__()

        self.model = model
        self.loss  = loss

    def forward(self, xs, ys, **kwargs):
        preds = self.model(*xs, **kwargs)
        loss_kp  = self.loss(preds, ys, **kwargs)
        return loss_kp

# for model backward compatibility
# previously model was wrapped by DataParallel module
class DummyModule(nn.Module):
    def __init__(self, model):
        super(DummyModule, self).__init__()
        self.module = model

    def forward(self, *xs, **kwargs):
        return self.module(*xs, **kwargs)


class NetworkFactory(object):
    def __init__(self, db):
        super(NetworkFactory, self).__init__()

        module_file = "models.{}".format(system_configs.snapshot_name)
        print("module_file: {}".format(module_file))
        nnet_module = importlib.import_module(module_file)

        self.model   = DummyModule(nnet_module.model(db))
        self.loss    = nnet_module.loss
        self.network = Network(self.model, self.loss)
        self.network = DataParallel(self.network, chunk_sizes=system_configs.chunk_sizes).cuda()
        self.load_cropped_pretrained_model("cache/nnet/CenterNet-52/CenterNet-52_480000.pkl")

        total_params = 0
        for params in self.model.parameters():
            num_params = 1
            for x in params.size():
                num_params *= x
            total_params += num_params
        print("total parameters: {}".format(total_params))

        self.fix_layers()  # fix kps and prelayer

        if system_configs.opt_algo == "adam":
            self.optimizer = torch.optim.Adam(
                filter(lambda p: p.requires_grad, self.model.parameters())
            )
        elif system_configs.opt_algo == "sgd":
            self.optimizer = torch.optim.SGD(
                filter(lambda p: p.requires_grad, self.model.parameters()),
                lr=system_configs.learning_rate, 
                momentum=0.9, weight_decay=0.0001
            )
        else:
            raise ValueError("unknown optimizer")

    def cuda(self):
        self.model.cuda()

    def load_cropped_pretrained_model(self, params_file):
        x = torch.load(params_file)
        params = {'module.model.%s'%k: v for k, v in x.items() if 'heats' not in k}
        self.network.load_state_dict(params, strict=False)
        print("load the cropped weights from COCO successfully.")

    def fix_layers(self):
        for m, v in self.network.named_parameters():
            if '.pre' in m or '.kps' in m:
                v.requires_grad = False

    def train_mode(self):
        self.network.train()

    def eval_mode(self):
        self.network.eval()

    def train(self, xs, ys, **kwargs):
        xs = [x for x in xs]
        ys = [y for y in ys]

        self.optimizer.zero_grad()
        loss_kp = self.network(xs, ys)
        loss        = loss_kp[0]
        focal_loss  = loss_kp[1]
        pull_loss   = loss_kp[2]
        push_loss   = loss_kp[3]
        regr_loss   = loss_kp[4]
        loss        = loss.mean()
        focal_loss  = focal_loss.mean()
        pull_loss   = pull_loss.mean()
        push_loss   = push_loss.mean()
        regr_loss   = regr_loss.mean()
        loss.backward()
        self.optimizer.step()
        return loss, focal_loss, pull_loss, push_loss, regr_loss

    def validate(self, xs, ys, **kwargs):
        with torch.no_grad():
            xs = [x.cuda(non_blocking=True) for x in xs]
            ys = [y.cuda(non_blocking=True) for y in ys]

            loss_kp = self.network(xs, ys)
            loss       = loss_kp[0]
            focal_loss = loss_kp[1]
            pull_loss  = loss_kp[2]
            push_loss  = loss_kp[3]
            regr_loss  = loss_kp[4]
            loss = loss.mean()
            return loss

    def test(self, xs, **kwargs):
        with torch.no_grad():
            xs = [x.cuda(non_blocking=True) for x in xs]
            return self.model(*xs, **kwargs)

    def set_lr(self, lr):
        print("setting learning rate to: {}".format(lr))
        for param_group in self.optimizer.param_groups:
            param_group["lr"] = lr

    def load_pretrained_params(self, pretrained_model):
        print("loading from {}".format(pretrained_model))
        with open(pretrained_model, "rb") as f:
            params = torch.load(f)
            self.model.load_state_dict(params)

    def load_params(self, iteration):
        cache_file = system_configs.snapshot_file.format(iteration)
        print("loading model from {}".format(cache_file))
        with open(cache_file, "rb") as f:
            params = torch.load(f)
            self.model.load_state_dict(params)

    def save_params(self, iteration):
        cache_file = system_configs.snapshot_file.format(iteration)
        print("saving model to {}".format(cache_file))
        with open(cache_file, "wb") as f:
            params = self.model.state_dict()
            torch.save(params, f) 

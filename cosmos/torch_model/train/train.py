"""
Training helper class
Takes a model, dataset, and training paramters
as arguments
"""
import torch
from  torch import nn
from os.path import join, isdir
from os import mkdir
import os
from torch import optim
from torch.utils.data import DataLoader, WeightedRandomSampler
from tqdm import tqdm
from train.anchor_targets.head_target_layer import HeadTargetLayer
from functools import partial
from tensorboardX import SummaryWriter


def unpack_cls(cls_dict, gt_label):
    label = cls_dict[gt_label]
    return torch.tensor([label])


def collate(batch, cls_dict):
    """
    collation function for GTDataset class
    :param batch:
    :return:
    """
    ex_windows = [item.ex_window for item in batch]
    gt_box = [item.gt_box for item in batch]
    gt_cls = [unpack_cls(cls_dict, item.gt_cls) for item in batch]
    proposals = [item.ex_proposal for item in batch]
    return torch.stack(ex_windows).float(), gt_box, torch.cat(gt_cls), proposals



def prep_gt_boxes(boxes, device):
    boxes = [box.reshape(1,-1, 4).float().to(device) for box in boxes]
    return boxes


class TrainerHelper:
    def __init__(self, model, train_set, val_set, params,device):
        """
        Initialize a trainer helper class
        :param model: a MMFasterRCNN model
        :param dataset: a GTDataset inheritor to load data from
        :param params: a dictionary of training specific parameters
        """
        self.model = model.to(device)
        self.train_set, self.val_set = train_set, val_set
        self.params = params
        self.cls = dict([(val, idx) for (idx, val) in enumerate(model.cls_names)])
        self.weight_vec = train_set.get_weight_vec(model.cls_names)
        self.device = device
        if params["USE_TENSORBOARD"]:
            self.writer = SummaryWriter()
        self.head_target_layer = HeadTargetLayer(
                                     ncls=len(model.cls_names)).to(device)

                                     

    def detect_weights(self,weights_dir):
        ls = os.listdir(weights_dir)
        if len(ls) == 0:
            return
        path = join(weights_dir, ls[len(ls) - 1])

    def train(self):
        optimizer = optim.Adam(self.model.parameters(), 
                              lr=self.params["LEARNING_RATE"],
                              weight_decay=self.params["WEIGHT_DECAY"])
        train_loader = DataLoader(self.train_set,
                            batch_size=int(self.params["BATCH_SIZE"]),
                            collate_fn=partial(collate,cls_dict=self.cls),
                            num_workers=int(self.params["BATCH_SIZE"]),
														sampler=WeightedRandomSampler(self.weight_vec, int(len(self.train_set) *.7)))
                            
        self.model.train(mode=True)
        iteration = 0
        for epoch in tqdm(range(int(self.params["EPOCHS"])),desc="epochs"):
            tot_cls_loss = 0.0
            for idx, batch in enumerate(tqdm(train_loader, desc="batches", leave=False)):
                optimizer.zero_grad()
                ex, gt_box, gt_cls, proposals = batch
                ex = ex.to(self.device)
                gt_cls = gt_cls.to(self.device)
                rois, cls_scores= self.model(ex, self.device, proposals=proposals)
                cls_loss = self.head_target_layer(cls_scores, gt_cls, self.device)
                loss = cls_loss
                tot_cls_loss += float(cls_loss)
                loss.backward()
                nn.utils.clip_grad_value_(self.model.parameters(), 5)
                optimizer.step()
            if epoch % self.params["CHECKPOINT_PERIOD"] == 0:
                name = f"model_{epoch}.pth"
                path = join(self.params["SAVE_DIR"], name)
                if not isdir(self.params["SAVE_DIR"]):
                    mkdir(self.params["SAVE_DIR"])
                torch.save(self.model.state_dict(), path)
            self.validate(iteration)
            self.writer.add_scalar("train_cls_loss", tot_cls_loss / len(self.train_set), iteration)
            iteration += 1

    def validate(self, iteration=0, to_tensorboard=True):
        loader = DataLoader(self.val_set,
                            batch_size=1,
                            collate_fn=partial(collate,cls_dict=self.cls),
                            num_workers=3)
        tot_cls_loss = 0.0
        self.model.train(mode=False)
        for batch in tqdm(loader, desc="validation"):
            ex, gt_box, gt_cls, proposals = batch
            ex = ex.to(self.device)
            gt_box = gt_box
            gt_cls = gt_cls.to(self.device)
            gt_box = prep_gt_boxes(gt_box, self.device)
            # forward pass
            rois, cls_scores, = self.model(ex, self.device, proposals=proposals)
            # calculate losses
            cls_loss = self.head_target_layer(
                    cls_scores, gt_cls, self.device)
            # update batch losses, cast as float so we don't keep gradient history
            tot_cls_loss += float(cls_loss)
        if to_tensorboard:
                self.output_batch_losses(
                                 tot_cls_loss/len(self.val_set),
                                 iteration)
        self.model.train(mode=True)
        return tot_cls_loss/len(self.val_set)



    def output_batch_losses(self,  cls_loss,iter ):
        """
        output either by priting or to tensorboard
        :param rpn_cls_loss:
        :param rpn_bbox_loss:
        :param cls_loss:
        :param bbox_loss:
        :return:
        """
        if self.params["USE_TENSORBOARD"]:
            vals = {
                "cls_loss": cls_loss,
            }
            for key in vals:
                self.writer.add_scalar(key, vals[key], iter)
        print(f"  head_cls_loss: {cls_loss}")


def check_grad(model):
    flag = False
    for param in model.parameters():
        if not(param.grad is None):
            if not(param.grad.data.sum() == 0):
                flag = True
    return flag


def save_weights(model):
    save = {}
    for key in model.state_dict():
        save[key] = model.state_dict()[key].clone()
    return save


def check_weight_update(old, new):
    flag = False
    for key in old.keys():
        if not (old[key] == new[key]).all():
            flag = True
    return flag
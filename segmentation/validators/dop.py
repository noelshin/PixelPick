from math import ceil

import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
from tqdm import tqdm

from datasets.camvid import CamVidDataset
from datasets.voc import VOC2012Segmentation
from criterions.dop import Criterion
from utils.metrics import prediction, eval_metrics
from utils.utils import AverageMeter, EmbeddingVisualiser, write_log, Visualiser


class Validator:
    def __init__(self, args):
        if args.dataset_name == "cv":
            dataset = CamVidDataset(args, val=True)
        elif args.dataset_name == "voc":
            dataset = VOC2012Segmentation(args, val=True)
        else:
            raise ValueError(args.dataset_name)

        self.debug = args.debug
        self.dataset_name = args.dataset_name
        self.dataloader = DataLoader(dataset, batch_size=1, num_workers=args.n_workers)
        self.device = torch.device("cuda:{:s}".format(args.gpu_ids) if torch.cuda.is_available() else "cpu:0")
        self.distance = Criterion.distance
        self.dir_checkpoints = f"{args.dir_root}/checkpoints/{args.experim_name}/val"
        self.experim_name = args.experim_name
        self.ignore_index = args.ignore_index
        self.log_val = args.log_val
        self.n_classes = args.n_classes
        self.non_isotropic = args.non_isotropic
        self.stride_total = args.stride_total
        self.use_softmax = args.use_softmax
        self.vis = Visualiser(self.dataset_name)

    def __call__(self, model, prototypes, epoch, dict_label_counts=None):
        model.eval()

        running_miou = AverageMeter()
        running_pixel_acc = AverageMeter()
        dataloader_iter = iter(self.dataloader)
        tbar = tqdm(range(len(self.dataloader)))
        fmt = "mIoU: {:.3f} | pixel acc.: {:.3f}"

        total_inter, total_union = 0, 0
        total_correct, total_label = 0, 0
        with torch.no_grad():
            for _ in tbar:
                dict_data = next(dataloader_iter)
                x, y = dict_data['x'].to(self.device), dict_data['y'].to(self.device)

                if self.dataset_name == "voc":
                    h, w = y.shape[1:]
                    pad_h = ceil(h / self.stride_total) * self.stride_total - x.shape[2]
                    pad_w = ceil(w / self.stride_total) * self.stride_total - x.shape[3]
                    x = F.pad(x, pad=(0, pad_w, 0, pad_h), mode='reflect')
                    dict_outputs = model(x)
                    k = 'pred' if self.use_softmax else 'emb'
                    dict_outputs[k] = dict_outputs[k][:, :, :h, :w]

                else:
                    dict_outputs = model(x)

                if self.use_softmax:
                    confidence = dict_outputs['pred']
                    pred = confidence.argmax(dim=1)
                    confidence = confidence.max(dim=1)[0]

                else:
                    emb = dict_outputs['emb']
                    pred, confidence = prediction(emb, prototypes, non_isotropic=self.non_isotropic,
                                                  return_confidence=True)

                    if self.dataset_name == "voc":
                        pred.flatten()[confidence.flatten() < 0.5] = -1
                        pred += 1

                # b x h x w
                correct, labeled, inter, union = eval_metrics(pred, y, self.n_classes, self.ignore_index)

                total_inter, total_union = total_inter + inter, total_union + union
                total_correct, total_label = total_correct + correct, total_label + labeled

                # PRINT INFO
                pix_acc = 1.0 * total_correct / (np.spacing(1) + total_label)
                IoU = 1.0 * total_inter / (np.spacing(1) + total_union)
                mIoU = IoU.mean()

                running_miou.update(mIoU)
                running_pixel_acc.update(pix_acc)
                tbar.set_description(fmt.format(running_miou.avg, running_pixel_acc.avg))

                if self.debug:
                    break

        print('\n' + '=' * 100)
        print("Experim name:", self.experim_name)
        print("Epoch {:d} | miou: {:.3f} | pixel_acc.: {:.3f}".format(epoch, running_miou.avg, running_pixel_acc.avg))
        print('=' * 100 + '\n')

        write_log(self.log_val, list_entities=[epoch, running_miou.avg, running_pixel_acc.avg])

        dict_tensors = {'input': dict_data['x'][0].cpu(),
                        'target': dict_data['y'][0].cpu(),
                        'pred': pred[0].detach().cpu(),
                        'confidence': -confidence[0].detach().cpu()}  # minus sign is to draw uncertain part bright

        self.vis(dict_tensors, fp=f"{self.dir_checkpoints}/{epoch}.png")

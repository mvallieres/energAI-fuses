from typing import Optional

import numpy as np
import torch
from torch.nn.utils import clip_grad_norm_
from torch.cuda.amp import autocast
from torch.cuda.amp.grad_scaler import GradScaler
import torchvision.models.detection as detection
from torch.cuda import memory_reserved, memory_allocated
from src.data.SummaryWriter import SummaryWriter
from torchvision.ops import box_iou, nms
from tqdm import tqdm

from constants import CLASS_DICT
from src.data.DataLoaderManager import DataLoaderManager
from src.models.EarlyStopper import EarlyStopper


class TrainValidTestManager:
    """
    Training, validation and testing manager.
    """

    def __init__(self, data_loader_manager: DataLoaderManager,
                 file_name: Optional[str],
                 model_name: str,
                 learning_rate: float,
                 weight_decay: float,
                 early_stopping,
                 mixed_precision,
                 gradient_accumulation,
                 pretrained: bool,
                 iou_threshold: float,
                 gradient_clip: float,
                 args_dic) -> None:
        self.args_dic = args_dic

        self.train_step = 0
        self.valid_step = 0
        self.total_step = 0

        self.gradient_clip = gradient_clip

        self.iou_threshold = iou_threshold

        self.pretrained = pretrained
        self.model_name = model_name

        self.writer = SummaryWriter('logdir/' + file_name)

        self.mixed_precision = mixed_precision
        self.accumulation_size = gradient_accumulation
        self.gradient_accumulation = False if gradient_accumulation == 1 else True

        self.num_classes = len(CLASS_DICT) + 1 # + 1 to include background class
        self.early_stopping = early_stopping

        if self.early_stopping is not None:
            self.early_stopper = EarlyStopper(patience=self.early_stopping, min_delta=0)

        self.scaler = GradScaler(enabled=self.mixed_precision)

        # Extract the training, validation and testing data loaders
        self.data_loader_train = data_loader_manager.data_loader_train
        self.data_loader_valid = data_loader_manager.data_loader_valid
        self.data_loader_test = data_loader_manager.data_loader_test
        # self.batch_size = data_loader_manager.batch_size
        print(f'=== Dataset & Data Loader Sizes ===\n\n'
              f'Training:\t\t{len(self.data_loader_train.dataset)} images\t\t{len(self.data_loader_train)} batches\n'
              f'Validation:\t\t{len(self.data_loader_valid.dataset)} images\t\t{len(self.data_loader_valid)} batches\n'
              f'Testing:\t\t{len(self.data_loader_test.dataset)} images\t\t{len(self.data_loader_test)} batches\n')
        # Save the file name
        self.file_name = file_name

        # Define device as the GPU if available, else use the CPU
        self.device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

        # Get model and set last fully-connected layer with the right number of classes
        self.load_model()

        # Define loss function
        # self.criterion = torch.nn.CrossEntropyLoss()

        # Find which parameters to train (those with .requires_grad = True)
        params = [p for p in self.model.parameters() if p.requires_grad]

        # Define Adam optimizer
        self.optimizer = torch.optim.Adam(params, lr=learning_rate, weight_decay=weight_decay)

        # Send the model to the device
        self.model.to(self.device)

        # Results dictionary
        # self.results = {
        #     'Training Loss': [],
        #     'Training Accuracy': [],
        #     'Validation Loss': [],
        #     'Validation Accuracy': []
        # }

    def __call__(self, epochs):
        self.train_model(epochs)
        self.test_model()

        self.writer.flush()
        self.writer.close()

    def train_model(self, epochs: int) -> None:
        for epoch in range(1, epochs + 1):
            loss = self.evaluate(self.data_loader_train, f'Training Epoch {epoch}', 'Training', self.train_step)
            self.save_epoch('Training', loss, None, None, epoch)

            # Validate the model
            metric = self.validate_model(epoch)

            if self.early_stopping and self.early_stopper.step(torch.as_tensor(metric, dtype=torch.float16)):
                print(f'Early stopping criterion has been reached for {self.early_stopping} epochs\n')
                break

        # If file_name is specified, save the trained model
        if self.file_name is not None:
            torch.save(self.model.state_dict(), f'models/{self.file_name}')

    def validate_model(self, epoch) -> float:
        """
        Method to validate the model saved in the self.model class attribute.

        :return: None
        """
        # pbar = tqdm(total=len(self.data_loader_valid), leave=False,
        #             desc=f'Validation Loss Epoch {epoch}')
        #
        # loss_list_epoch = []
        #
        # # Deactivate the autograd engine
        # with torch.no_grad():
        #     for i, (images, targets) in enumerate(self.data_loader_valid):
        #         images = torch.stack(images).to(self.device)
        #         targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets]
        #
        #         loss_dict = self.model(images, targets)
        #         losses = sum(loss for loss in loss_dict.values())
        #
        #         loss_list_epoch.append(losses.item())
        #
        #         self.valid_loss_step = self.save_batch_loss('Validation', losses, self.valid_loss_step)
        #         self.save_memory()
        #
        #         # Update progress bar
        #         pbar.set_postfix_str(f'Loss: {losses:.5f}')
        #         pbar.update()
        #
        # pbar.close()
        #
        # loss = np.mean(loss_list_epoch)
        loss = self.evaluate(self.data_loader_valid, f'Validation Epoch {epoch}', 'Validation', self.valid_step)
        recall, precision = self.predict(self.data_loader_valid, f'Validation Metrics Epoch {epoch}')

        self.save_epoch('Validation', loss, recall, precision, epoch)

        return recall

    def test_model(self) -> None:
        recall, precision = self.predict(self.data_loader_test, 'Testing Metrics')

        print('=== Testing Results ===\n')
        print(f'Recall (mean per image):\t\t\t{recall:.2%}')
        print(f'Precision (mean per image):\t\t\t{precision:.2%}\n')

        metrics_dict = {
            'hparam/Recall': recall,
            'hparam/Precision': precision
        }

        self.writer.add_hparams(self.args_dic, metric_dict=metrics_dict)

    def evaluate(self, data_loader, phase, epoch, step):
        # Declare tqdm progress bar
        pbar = tqdm(total=len(data_loader), leave=False, desc=f'{phase} Epoch {epoch}')

        # Specify that the model will be trained
        self.model.train()

        loss_list_epoch = []

        if self.gradient_accumulation:
            self.optimizer.zero_grad()

        for i, (images, targets) in enumerate(data_loader):
            images = torch.stack(images).to(self.device)
            targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets]

            with autocast(enabled=self.mixed_precision):
                loss_dict = self.model(images, targets)
                losses = sum(loss for loss in loss_dict.values())

            if not self.gradient_accumulation and not self.mixed_precision:
                self.optimizer.zero_grad()
                losses.backward()
                self.optimizer.step()

            elif not self.gradient_accumulation and self.mixed_precision:
                self.scaler.scale(losses).backward()
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad(set_to_none=True)

            elif self.gradient_accumulation and not self.mixed_precision:
                losses.backward()

                if (i + 1) % self.accumulation_size == 0:
                    clip_grad_norm_(self.model.parameters(), max_norm=self.gradient_clip)
                    self.optimizer.step()
                    self.optimizer.zero_grad()

            elif self.gradient_accumulation and self.mixed_precision:
                self.scaler.scale(losses).backward()

                if (i + 1) % self.accumulation_size == 0:
                    self.scaler.unscale_(self.optimizer)
                    clip_grad_norm_(self.model.parameters(), max_norm=self.gradient_clip)
                    self.scaler.step(self.optimizer)
                    self.scaler.update()
                    self.optimizer.zero_grad(set_to_none=True)

            loss_list_epoch.append(losses.item())

            step = self.save_batch_loss(phase, losses, step)
            self.save_memory()

            # Update progress bar
            pbar.set_postfix_str(f'Loss: {losses:.5f}')
            pbar.update()

        pbar.close()

        return np.mean(loss_list_epoch)

    def predict(self, data_loader, desc):
        pbar = tqdm(total=len(data_loader), leave=False, desc=desc)

        self.model.eval()

        preds_list = []
        targets_list = []

        # Deactivate the autograd engine
        with torch.no_grad():
            for images, targets in data_loader:
                images = torch.stack(images).to(self.device)
                targets = [{k: v.to(self.device) for k, v in t.items()} for t in targets]

                preds = self.model(images)

                preds_list += preds
                targets_list += targets

                self.save_memory()
                pbar.update()

        pbar.close()

        # preds_list = self.apply_nms(preds_list)

        return self.calculate_metrics(preds_list, targets_list)

    def apply_nms(self, preds_list):
        keep_nms = [nms(pred['boxes'], pred['scores'], self.iou_threshold) for pred in preds_list]

        preds_nms = []

        for pred, keep in zip(preds_list, keep_nms):
            preds_nms.append({key: torch.index_select(val, dim=0, index=keep) for key, val in pred.items()})

        return preds_nms

    def calculate_metrics(self, preds_list, targets_list):
        targets_boxes = [target['boxes'] for target in targets_list]
        targets_labels = [target['labels'] for target in targets_list]
        preds_boxes = [pred['boxes'] for pred in preds_list]
        preds_labels = [pred['labels'] for pred in preds_list]

        iou_list = []

        for pred_boxes, target_boxes in zip(preds_boxes, targets_boxes):
            iou_list.append(box_iou(pred_boxes, target_boxes))

        max_iou_list = []
        types_list = []

        for iou, target_labels, pred_labels in zip(iou_list, targets_labels, preds_labels):
            if iou.nelement() > 0:
                max_iou_list.append(torch.max(iou, dim=1))

                type_list_iter = []

                for i, (value, index) in enumerate(zip(max_iou_list[-1].values, max_iou_list[-1].indices)):
                    if torch.greater(value, self.iou_threshold) and \
                            torch.equal(target_labels.data[index], pred_labels.data[i]):
                        type_list_iter.append(True)
                    else:
                        type_list_iter.append(False)

                types_list.append(type_list_iter)
            else:
                types_list.append([])

        recall_list = [sum(types) / len(targets_labels[i]) for i, types in enumerate(types_list)]
        precision_list = [0 if len(types) == 0 else sum(types) / len(types) for types in types_list]

        return np.mean(recall_list), np.mean(precision_list)

    def load_model(self,
                   progress: bool = True,
                   pretrained_backbone: bool = True,
                   trainable_backbone_layers: Optional[int] = None):
        """
        pretrained (bool) – If True, returns a model pre-trained on COCO train2017
        progress (bool) – If True, displays a progress bar of the download to stderr
        num_classes (int) – number of output classes of the model (including the background)
        pretrained_backbone (bool) – If True, returns a model with backbone pre-trained on Imagenet
        trainable_backbone_layers (int) – number of trainable (not frozen) resnet layers starting from final block.
        Valid values are between 0 and 5, with 5 meaning all backbone layers are trainable.

        :param name:
        :param num_classes:
        :param pretrained:
        :param progress:
        :param pretrained_backbone:
        :param trainable_backbone_layers:
        :return:
        """
        # model = detection.fasterrcnn_resnet50_fpn(pretrained=False)
        # # get number of input features for the classifier
        # in_features = model.roi_heads.box_predictor.cls_score.in_features
        # # replace the pre-trained head with a new one
        # model.roi_heads.box_predictor = detection.faster_rcnn.FastRCNNPredictor(in_features, self.num_classes)
        #
        # self.model = model
        if self.model_name == 'fasterrcnn_resnet50_fpn':
            self.model = detection.fasterrcnn_resnet50_fpn(pretrained=self.pretrained,
                                                           progress=progress,
                                                           pretrained_backbone=pretrained_backbone,
                                                           trainable_backbone_layers=
                                                           trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'fasterrcnn_mobilenet_v3_large_fpn':
            self.model = detection.fasterrcnn_mobilenet_v3_large_fpn(pretrained=self.pretrained,
                                                                     progress=progress,
                                                                     pretrained_backbone=pretrained_backbone,
                                                                     trainable_backbone_layers=
                                                                     trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'fasterrcnn_mobilenet_v3_large_320_fpn':
            self.model = detection.fasterrcnn_mobilenet_v3_large_320_fpn(pretrained=self.pretrained,
                                                                         progress=progress,
                                                                         # num_classes=self.num_classes,
                                                                         pretrained_backbone=pretrained_backbone,
                                                                         trainable_backbone_layers=
                                                                         trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'retinanet_resnet50_fpn':
            self.model = detection.retinanet_resnet50_fpn(pretrained=self.pretrained,
                                                          progress=progress,
                                                          pretrained_backbone=pretrained_backbone,
                                                          trainable_backbone_layers=
                                                          trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'maskrcnn_resnet50_fpn':
            self.model = detection.maskrcnn_resnet50_fpn(pretrained=self.pretrained,
                                                         progress=progress,
                                                         pretrained_backbone=pretrained_backbone,
                                                         trainable_backbone_layers=
                                                         trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'keypointrcnn_resnet50_fpn':
            self.model = detection.keypointrcnn_resnet50_fpn(pretrained=self.pretrained,
                                                             progress=progress,
                                                             pretrained_backbone=pretrained_backbone,
                                                             trainable_backbone_layers=
                                                             trainable_backbone_layers)
            self.replace_model_head()

        elif self.model_name == 'detr':
            """
            To Do: Implement DETR

            Paper:              https://arxiv.org/abs/2005.12872
            Official Repo:      https://github.com/facebookresearch/detr
            Unofficial Repo:    https://github.com/clive819/Modified-DETR
            """
            raise NotImplementedError

        elif self.model_name == 'perceiver':
            """
            To Do : Implement Perceiver

            Paper:              https://arxiv.org/abs/2103.03206
            Unofficial Repo:    https://github.com/lucidrains/perceiver-pytorch
            Unofficial Repo:    https://github.com/louislva/deepmind-perceiver
            """
            raise NotImplementedError

    def replace_model_head(self):
        in_features = self.model.roi_heads.box_predictor.cls_score.in_features

        if 'fasterrcnn' in self.model_name:
            self.model.roi_heads.box_predictor = detection.faster_rcnn.FastRCNNPredictor(in_channels=in_features,
                                                                                         num_classes=self.num_classes)

        elif 'retinanet' in self.model_name:
            self.model.roi_heads.box_predictor = detection.retinanet.RetinaNetHead(in_features=in_features,
                                                                                   num_classes=self.num_classes)

        elif 'maskrcnn' in self.model_name:
            # from .mask_rcnn import *
            raise NotImplementedError

        elif 'keypointrcnn' in self.model_name:
            # from .keypoint_rcnn import *
            raise NotImplementedError

        else:
            raise NotImplementedError

    def save_batch_loss(self, bucket, loss, step):
        self.writer.add_scalar(f'Loss (total per batch)/{bucket}', loss, step)

        return step + 1

    def save_epoch(self, bucket, loss, recall, precision, epoch):
        self.writer.add_scalar(f'Loss (mean per epoch)/{bucket}', loss, epoch)

        if recall is not None:
            self.writer.add_scalar(f'Recall (mean per epoch)/{bucket}', recall, epoch)

        if precision is not None:
            self.writer.add_scalar(f'Precision (mean per epoch)/{bucket}', precision, epoch)

    def save_memory(self, scale=1e-9):
        mem_reserved = memory_reserved(0) * scale
        mem_allocated = memory_allocated(0) * scale
        mem_free = mem_reserved - mem_allocated

        self.writer.add_scalar('Memory/Reserved', mem_reserved, self.total_step)
        self.writer.add_scalar('Memory/Allocated', mem_allocated, self.total_step)
        self.writer.add_scalar('Memory/Free', mem_free, self.total_step)

        self.total_step += 1

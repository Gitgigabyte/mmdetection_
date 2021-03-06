import torch
import torch.nn.functional as F

from mmdet.core import bbox2roi, build_assigner, build_sampler, bbox2result
from .. import builder
from ..registry import DETECTORS
from .two_stage import TwoStageDetector


@DETECTORS.register_module
class MaskHintRCNN(TwoStageDetector):
    """Mask Scoring RCNN.

    https://arxiv.org/abs/1903.00241
    """

    def __init__(self,
                 backbone,
                 rpn_head,
                 bbox_roi_extractor,
                 bbox_head,
                 mask_roi_extractor,
                 mask_head,
                 train_cfg,
                 test_cfg,
                 neck=None,
                 shared_head=None,
                 mask_iou_head=None,
                 pretrained=None):
        super(MaskHintRCNN, self).__init__(
            backbone=backbone,
            neck=neck,
            shared_head=shared_head,
            rpn_head=rpn_head,
            bbox_roi_extractor=bbox_roi_extractor,
            bbox_head=bbox_head,
            mask_roi_extractor=mask_roi_extractor,
            mask_head=mask_head,
            train_cfg=train_cfg,
            test_cfg=test_cfg,
            pretrained=pretrained)
        self.mask_iou_head = builder.build_head(mask_iou_head)
        self.mask_iou_head.init_weights()

    def forward_dummy(self, img):
        raise NotImplementedError

    # TODO: refactor forward_train in two stage to reduce code redundancy
    def forward_train(self,
                      img,
                      img_meta,
                      gt_bboxes,
                      gt_labels,
                      gt_bboxes_ignore=None,
                      gt_masks=None,
                      proposals=None):
        x = self.extract_feat(img)

        losses = dict()

        # RPN forward and loss
        if self.with_rpn:
            rpn_outs = self.rpn_head(x)
            rpn_loss_inputs = rpn_outs + (gt_bboxes, img_meta,
                                          self.train_cfg.rpn)
            rpn_losses = self.rpn_head.loss(
                *rpn_loss_inputs, gt_bboxes_ignore=gt_bboxes_ignore)
            losses.update(rpn_losses)

            proposal_cfg = self.train_cfg.get('rpn_proposal',
                                              self.test_cfg.rpn)
            proposal_inputs = rpn_outs + (img_meta, proposal_cfg)
            proposal_list = self.rpn_head.get_bboxes(*proposal_inputs)
        else:
            proposal_list = proposals

        # assign gts and sample proposals
        if self.with_bbox or self.with_mask:
            bbox_assigner = build_assigner(self.train_cfg.rcnn.assigner)
            bbox_sampler = build_sampler(
                self.train_cfg.rcnn.sampler, context=self)
            num_imgs = img.size(0)
            if gt_bboxes_ignore is None:
                gt_bboxes_ignore = [None for _ in range(num_imgs)]
            sampling_results = []
            for i in range(num_imgs):
                assign_result = bbox_assigner.assign(proposal_list[i],
                                                     gt_bboxes[i],
                                                     gt_bboxes_ignore[i],
                                                     gt_labels[i])
                sampling_result = bbox_sampler.sample(
                    assign_result,
                    proposal_list[i],
                    gt_bboxes[i],
                    gt_labels[i],
                    feats=[lvl_feat[i][None] for lvl_feat in x])
                sampling_results.append(sampling_result)

        # bbox head forward and loss
        if self.with_bbox:
            rois = bbox2roi([res.bboxes for res in sampling_results])
            # TODO: a more flexible way to decide which feature maps to use
            bbox_feats = self.bbox_roi_extractor(
                x[:self.bbox_roi_extractor.num_inputs], rois)

            if self.with_shared_head:
                bbox_feats = self.shared_head(bbox_feats)
            # mask_targets, bg_targets = self.mask_iou_head.get_mask_target(sampling_results,gt_masks,self.train_cfg.rcnn)
            # cls_refine, bbox_refine = self.mask_iou_head(bbox_feats, bg_targets, mask_targets)
            cls_score, bbox_pred = self.bbox_head(bbox_feats)

            bbox_targets = self.bbox_head.get_target(sampling_results,
                                                     gt_bboxes, gt_labels,
                                                     self.train_cfg.rcnn)
            # loss_bbox_refine = self.mask_iou_head(cls_refine, bbox_refine, *bbox_targets)
            loss_bbox = self.bbox_head.loss(cls_score, bbox_pred,
                                            *bbox_targets)
            losses.update(loss_bbox)
            # losses.update(loss_bbox_refine)

        # mask head forward and loss
        if self.with_mask:
            if not self.share_roi_extractor:
                pos_rois = bbox2roi(
                    [res.pos_bboxes for res in sampling_results])
                mask_feats = self.mask_roi_extractor(
                    x[:self.mask_roi_extractor.num_inputs], pos_rois)
                if self.with_shared_head:
                    mask_feats = self.shared_head(mask_feats)
            else:
                pos_inds = []
                device = bbox_feats.device
                for res in sampling_results:
                    pos_inds.append(
                        torch.ones(
                            res.pos_bboxes.shape[0],
                            device=device,
                            dtype=torch.uint8))
                    pos_inds.append(
                        torch.zeros(
                            res.neg_bboxes.shape[0],
                            device=device,
                            dtype=torch.uint8))
                pos_inds = torch.cat(pos_inds)
                mask_feats = bbox_feats[pos_inds]
            mask_pred = self.mask_head(mask_feats)

            mask_targets, bg_targets = self.mask_head.get_target(sampling_results,
                                                     gt_masks,
                                                     self.train_cfg.rcnn)

            pos_labels = torch.cat(
                [res.pos_gt_labels for res in sampling_results])
            loss_mask = self.mask_head.loss(mask_pred, mask_targets, bg_targets,
                                            pos_labels, self.test_cfg.rcnn)
            losses.update(loss_mask)

            # mask iou head forward and loss
            # pos_mask_pred = mask_pred[range(mask_pred.size(0)), pos_labels]
            # if self.using_refine:
            # mask_targets, bg_targets = self.bbox_head.get_mask_target(sampling_results, gt_masks, self.train_cfg.rcnn)
            if self.train_cfg.rcnn.refine_sample == 'resample':
                refine_feats = self.bbox_roi_extractor(x[:self.bbox_roi_extractor.num_inputs], pos_rois)
            elif self.train_cfg.rcnn.refine_sample == 'interpolate':
                refine_feats = F.interpolate(mask_feats, (7,7))
            else:
                refine_feats = F.max_pool2d(mask_feats)
            mask_pred = (mask_pred.detach()[:,1:,:,:] >= self.train_cfg.rcnn.mask_thr_binary).float()
            refine_cls, refine_reg = self.mask_iou_head(refine_feats, mask_pred)
            refine_targets = self.mask_iou_head.get_target(sampling_results,
                                                           gt_bboxes, gt_labels,
                                                           self.train_cfg.rcnn)
            loss_refine = self.mask_iou_head.loss(refine_cls, refine_reg, *refine_targets)

            losses.update(loss_refine)

            # pos_mask_iou_pred = mask_iou_pred[range(mask_iou_pred.size(0)
            #                                         ), pos_labels]
            # mask_iou_targets = self.mask_iou_head.get_target(
            #     sampling_results, gt_masks, pos_mask_pred, mask_targets,
            #     self.train_cfg.rcnn)
            # bbox_targets = self.mask_iou_head.get_target(sampling_results,
            #                                          gt_bboxes, gt_labels,
            #                                          self.train_cfg.rcnn)
            # loss_bbox = self.bbox_head.loss(cls_score, bbox_pred,
            #                                 *bbox_targets)
            # losses.update(loss_bbox)
            # loss_mask_iou = self.mask_iou_head.loss(mask_refine_mask,mask_targets, self.test_cfg.rcnn)
            # losses.update(loss_mask_iou)
        return losses

    def simple_test_mask(self,
                         x,
                         img_meta,
                         det_bboxes,
                         det_labels,
                         rescale=False):
        # image shape of the first image in the batch (only one)
        ori_shape = img_meta[0]['ori_shape']
        scale_factor = img_meta[0]['scale_factor']

        if det_bboxes.shape[0] == 0:
            segm_result = [[] for _ in range(self.mask_head.num_classes - 1)]
            # mask_scores = [[] for _ in range(self.mask_head.num_classes - 1)]
        else:
            # if det_bboxes is rescaled to the original image size, we need to
            # rescale it back to the testing scale to obtain RoIs.
            _bboxes = (
                det_bboxes[:, :4] * scale_factor if rescale else det_bboxes)
            mask_rois = bbox2roi([_bboxes])
            mask_feats = self.mask_roi_extractor(
                x[:len(self.mask_roi_extractor.featmap_strides)], mask_rois)
            if self.with_shared_head:
                mask_feats = self.shared_head(mask_feats)
            mask_pred = self.mask_head(mask_feats)
            segm_result = self.mask_head.get_seg_masks(mask_pred, _bboxes,
                                                       det_labels,
                                                       self.test_cfg.rcnn,
                                                       ori_shape, scale_factor,
                                                       rescale)
            # get mask scores with mask iou head
            # refine_mask = self.mask_iou_head(
            #     mask_feats,
            #     mask_pred)
            # mask_scores = self.mask_iou_head.get_mask_scores(
            #     mask_iou_pred, det_bboxes, det_labels)
        return segm_result

    def refine_test_bboxes(self,
                           x,
                           img_meta,
                           proposals,
                           rcnn_test_cfg,
                           rescale=False):
        rois = bbox2roi(proposals)
        roi_feats = self.mask_roi_extractor(
            x[:self.mask_roi_extractor.num_inputs], rois)
        mask_pred = self.mask_head(roi_feats)[:,1:,:,:]
        if self.test_cfg.rcnn.refine_sample == 'resample':
            refine_feats = self.bbox_roi_extractor(x[:self.bbox_roi_extractor.num_inputs], rois)
        elif self.test_cfg.rcnn.refine_sample == 'interpolate':
            refine_feats = F.interpolate(roi_feats, (7, 7))
        else:
            refine_feats = F.max_pool2d(roi_feats)

        if self.with_shared_head:
            refine_feats = self.shared_head(refine_feats)
        # roi_feats = self.context_head(roi_feats, mask_feats)
        refine_masks = (mask_pred > rcnn_test_cfg.rcnn.mask_thr_binary).float()
        cls_score, bbox_pred = self.mask_iou_head(refine_feats,refine_masks)
        img_shape = img_meta[0]['img_shape']
        scale_factor = img_meta[0]['scale_factor']
        det_bboxes, det_labels = self.bbox_head.get_det_bboxes(
            rois,
            cls_score,
            bbox_pred,
            img_shape,
            scale_factor,
            rescale=rescale,
            cfg = rcnn_test_cfg
        )
        return det_bboxes, det_labels

    def simple_test(self, img, img_meta, proposals=None, rescale=False):
        """Test without augmentation."""
        assert self.with_bbox, "Bbox head must be implemented."

        x = self.extract_feat(img)

        proposal_list = self.simple_test_rpn(
            x, img_meta, self.test_cfg.rpn) if proposals is None else proposals
        det_bboxes, det_labels = self.simple_test_bboxes(
            x, img_meta, proposal_list, self.test_cfg.rcnn, rescale=rescale
        )
        # bbox_results = bbox2result(det_bboxes, det_labels,
        #                            self.bbox_head.num_classes)
        det_bboxes, det_labels = self.refine_test_bboxes(
            x, img_meta, det_bboxes, self.test_cfg.rcnn, rescale=rescale)

        bbox_results = bbox2result(det_bboxes, det_labels,
                                   self.bbox_head.num_classes)

        if not self.with_mask:
            return bbox_results
        else:
            segm_results = self.simple_test_mask(
                x, img_meta, det_bboxes, det_labels, rescale=rescale)
            return bbox_results, segm_results

import numpy as np
import torch
from torch.nn import functional as F


def _nms(heat, kernel=3):
    """Process heat with Non Maximum Suppression.

    Args:
        heat (torch.Tensor): Heatmap from model in shape (batch_size *
            channels * height * width)
        kernel (int): Kernel size for max pooling

    Returns:
        torch.Tensor: Heatmap after NMS, in shape (batch_size * channels *
        height * width)
    """
    pad = (kernel - 1) // 2
    hmax = F.max_pool2d(heat, (kernel, kernel), stride=1, padding=pad)
    keep = (hmax == heat).float()
    return heat * keep


def _topk_channel(scores, K=40):
    """Select top k channels of scores, index, y's, and x's given scores.

    Args:
        scores (torch.Tensor): Scores from heatmap key points parameters, in
            shape (batch_size * channels * height * width)
        K (int): The number of top values to be output

    Returns:
        tuple[torch.Tensor]: Top-k scores, indices, y and x coordinates
    """
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (topk_inds / width).int().float()
    topk_xs = (topk_inds % width).int().float()

    return topk_scores, topk_inds, topk_ys, topk_xs


def _gather_feat(feat, ind, mask=None):
    """Gather features with assigned index, with index expanded to the third
    dimension.

    Args:
        feat (torch.Tensor): Features from model in shape (batch_size *
            channels * height * width)
        ind (torch.Tensor): Index of features to be gathered

    Returns:
        torch.Tensor: gathered features, in shape (batch_size * channels *
        height * width)
    """

    # expand ind with the third dimension to be the same as feat
    dim = feat.size(2)
    ind = ind.unsqueeze(2).expand(ind.size(0), ind.size(1), dim)
    feat = feat.gather(1, ind)
    if mask is not None:
        mask = mask.unsqueeze(2).expand_as(feat)
        feat = feat[mask]
        feat = feat.view(-1, dim)
    return feat


def _permute_and_gather_feat(feat, ind):
    """Gather features by assigned index, with features permuted with (0,2,3,1)
    and reshaped to three dimensions.

    Args:
        feat (torch.Tensor): Features from model in shape (batch_size *
            channels * height * width)
        ind (torch.Tensor): Index of features to be gathered

    Returns:
        torch.Tensor: Permuted and gathered features, in shape (batch_size *
        channels * height * width)
    """
    feat = feat.permute(0, 2, 3, 1)
    feat = feat.view(feat.size(0), -1, feat.size(3))
    feat = _gather_feat(feat, ind)
    return feat


def _topk(scores, K=40):
    """Select top k batches of scores, index, classes, y's, and x's given
    scores.

    Args:
        scores (torch.Tensor): Scores from heatmap key points parameters, in
            shape (batch_size * channels * height * width)
        K (int): The number of top values to be output

    Returns:
        tuple[torch.Tensor]: Top-k scores, indices, classes, y and x
        coordinates
    """
    batch, cat, height, width = scores.size()

    topk_scores, topk_inds = torch.topk(scores.view(batch, cat, -1), K)

    topk_inds = topk_inds % (height * width)
    topk_ys = (topk_inds / width).int().float()
    topk_xs = (topk_inds % width).int().float()

    topk_score, topk_ind = torch.topk(topk_scores.view(batch, -1), K)
    topk_clses = (topk_ind / K).int()
    topk_inds = _gather_feat(topk_inds.view(batch, -1, 1),
                             topk_ind).view(batch, K)
    topk_ys = _gather_feat(topk_ys.view(batch, -1, 1), topk_ind).view(batch, K)
    topk_xs = _gather_feat(topk_xs.view(batch, -1, 1), topk_ind).view(batch, K)

    return topk_score, topk_inds, topk_clses, topk_ys, topk_xs


def transform_img2cam(kps, dim, rot, meta, const):
    """Transform coordinates in the 2D image plane to the 3D camera coordinate
    system.

    Please refer to `KM3D paper <https://arxiv.org/abs/2009.00764v1>`_ for
    detailed explanations of matrix.

    Matrix construction:
    matrix_left is the left-hand matrix with (-1 0 kp_1x) as the first row;
    matrix right is aimed to be the right-hand matrix with
    (l*cosµ)/2+(w*sinµ)/2-kp_1x*[-(l*sinµ)/2+(w*cosµ)/2] as the first row.
    By construction, data_B is the constant involving (l*cosµ)/2+(w*sinµ)/2
    and its kind; data_C is the constant involving -(l*sinµ)/2+(w*cosµ)/2
    and its kind.
    Therefore, matrix_B - kp_norm * matrix_C results in the final matrix.

    Args:
        kps (torch.Tensor): Key points in shape (batch_size * boxes * 18)
        dim (int): Dimensions of height, width, length in shape (batch_size *
            boxes * 3)
        rot (torch.Tensor): Rotation values in shape(batch_size * boxes * 8)
        meta (dict): should contain keys below.

            - trans_output_inv: Transition parameters for computing
                matrix_left in shape (1, 2, 3)
            - calib: Calibration for computing matrix_left in shape (1, 2, 3)
        const (torch.Tensor): constants in shape (batch_size * boxes * 16 * 2)

    Returns:
        torch.Tensor: pinv, coordinates in world frame
        torch.Tensor: rot_y, yaw angle in shape (batch_size * boxes * 1)
        torch.Tensor: kps, key points coordinates in shape (batch_size * boxes
        * 18)
    """
    batch = kps.size(0)
    boxes = kps.size(1)
    out_inv = meta['trans_output_inv']
    calib = meta['calib']

    out_inv = out_inv.unsqueeze(1)
    out_inv = out_inv.expand(batch, boxes, -1, -1).contiguous().view(-1, 2, 3)\
        .float()
    # Transform kps in shape (batch boxes 2 9), hom in shape (batch boxes 1 9)
    kps = kps.view(batch, boxes, -1, 2).permute(0, 1, 3, 2)
    hom = kps.new_ones(batch, boxes, 1, 9)
    kps = torch.cat((kps, hom), dim=2).view(-1, 3, 9)

    # kps in shape (batch boxes 2 9), i.e. [[x, x, x, x,...],[y, y, y, y,...]]
    kps = torch.bmm(out_inv, kps).view(batch, boxes, 2, 9)

    # kps in shape (batch boxes 18), i.e. [x, y, x, y, x, y,....]
    kps = kps.permute(0, 1, 3, 2).view(batch, boxes, -1)

    # calib_combined in shape (batch boxes 1)
    calib_combined = torch.zeros_like(kps[:, :, 0:1]) + calib[:, 0:1, 0:1]
    alpha_idx = rot[:, :, 1] > rot[:, :, 5]
    alpha_idx = alpha_idx.float()

    # alpha1, alpha_idx, alpna_pre in shape (batch boxes)
    alpha1 = torch.atan(rot[:, :, 2] / rot[:, :, 3]) + (-0.5 * np.pi)
    alpha2 = torch.atan(rot[:, :, 6] / rot[:, :, 7]) + (0.5 * np.pi)
    alpna_pre = alpha1 * alpha_idx + alpha2 * (1 - alpha_idx)
    alpna_pre = alpna_pre.unsqueeze(2)

    # rot_y in shape (batch boxes 1), which is the yaw angle
    rot_y = alpna_pre + torch.atan2(kps[:, :, 16:17] - calib[:, 0:1, 2:3],
                                    calib_combined)
    rot_y[rot_y > np.pi] = rot_y[rot_y > np.pi] - 2 * np.pi
    rot_y[rot_y < -np.pi] = rot_y[rot_y < -np.pi] + 2 * np.pi

    # transform calib into shape (batch boxes 3 4)
    calib = calib.unsqueeze(1)
    calib = calib.expand(batch, boxes, -1, -1).contiguous()
    kpoint = kps[:, :, :16]
    f = calib[:, :, 0, 0].unsqueeze(2)
    f = f.expand_as(kpoint)

    # cx, cy in shape (batch boxes 1), kp_norm in shape (1 boxes 16)
    cx, cy = calib[:, :, 0, 2].unsqueeze(2), calib[:, :, 1, 2].unsqueeze(2)
    cxy = torch.cat((cx, cy), dim=2)
    cxy = cxy.repeat(1, 1, 8)
    kp_norm = (kpoint - cxy) / f

    # following variables in shape (batch boxes 1)
    height = dim[:, :, 0:1]
    width = dim[:, :, 1:2]
    length = dim[:, :, 2:3]
    cos_rot_y = torch.cos(rot_y)
    sin_rot_y = torch.sin(rot_y)

    # initialize matrices in shape (batch boxes 16)
    matrix_B = torch.zeros_like(kpoint)
    matrix_C = torch.zeros_like(kpoint)

    # matrix_left in shape (batch boxes 16 3)
    kp = kp_norm.unsqueeze(3)
    const = const.expand(batch, boxes, -1, -1)
    matrix_left = torch.cat([const, kp], dim=3)

    # Below is the matrix computation for position solving in KM3D paper.
    data_B = torch.FloatTensor([
        length * 0.5 * cos_rot_y + width * 0.5 * sin_rot_y, height * 0.5,
        length * 0.5 * cos_rot_y - width * 0.5 * sin_rot_y, height * 0.5,
        -length * 0.5 * cos_rot_y - width * 0.5 * sin_rot_y, height * 0.5,
        -length * 0.5 * cos_rot_y + width * 0.5 * sin_rot_y, height * 0.5,
        length * 0.5 * cos_rot_y + width * 0.5 * sin_rot_y, -height * 0.5,
        length * 0.5 * cos_rot_y - width * 0.5 * sin_rot_y, -height * 0.5,
        -length * 0.5 * cos_rot_y - width * 0.5 * sin_rot_y, -height * 0.5,
        -length * 0.5 * cos_rot_y + width * 0.5 * sin_rot_y, -height * 0.5
    ])

    matrix_B[:, :, :] = data_B

    data_C = torch.FloatTensor([
        -length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        -length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y - width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y,
        length * 0.5 * sin_rot_y + width * 0.5 * cos_rot_y
    ])

    matrix_C[:, :, :] = data_C

    # final matrix_B in shape (1 boxes 16)
    matrix_right = matrix_B - kp_norm * matrix_C

    # make matrix_left_transposed in shape(batch * boxes 3 16)
    matrix_left_transposed = matrix_left.permute(0, 1, 3, 2)
    matrix_left_transposed = matrix_left_transposed.view(batch * boxes, 3, 16)

    matrix_left = matrix_left.view(batch * boxes, 16, 3)
    matrix_right = matrix_right.view(batch * boxes, 16, 1).float()

    # points_3d here in shape (batch * boxes 3 3)
    points_3d = torch.bmm(matrix_left_transposed, matrix_left)
    points_3d_inv = torch.inverse(points_3d)

    # points_3d_inv here in shape (batch * boxes 3 16)
    points_3d_inv = torch.bmm(points_3d_inv, matrix_left_transposed)

    # points_3d_inv here in shape (batch * boxes 3 1)
    points_3d_inv = torch.bmm(points_3d_inv, matrix_right)

    # final points_3d_inv in shape (batch, boxes, 3)
    points_3d_inv = points_3d_inv.view(batch, boxes, 3, 1).squeeze(3)

    return points_3d_inv, rot_y, kps


def object_pose_decode(heat,
                       wh,
                       kps,
                       dim,
                       rot,
                       prob=None,
                       reg=None,
                       kps_heatmap=None,
                       kps_offsets=None,
                       K=100,
                       meta=None,
                       const=None):
    """The entry for decoding object's pose with final bboxes, scores, key
    points, dimensions, heatmap score, yaw angle, position, probability, and
    classes.

    Args:
        heat (torch.Tensor): Predictions from model in shape (batch_size *
            channels * height * width)
        wh (torch.Tensor): Widths and heights of bboxs
        kps (torch.Tensor): Key points in shape (1 * batch_size * 18)
        dim (int): dimensions of height, width, length in shape (1 * batch_size
            * 3)
        rot (torch.Tensor): Rotation values in shape (1* batch_size * 8)
        prob (torch.Tensor): Probabilities
        reg (torch.Tensor): Reg parameters
        kps_heatmap (torch.Tensor): Heatmap key points parameters
        kps_offsets (torch.Tensor): Heatmap key points offsets for downsampling
            error's makeup
        K (int): Top K value
        meta (dict): should contain keys below.

            - trans_output_inv (torch.Tensor): Transition parameters for
                computing matrix_left in pinv's matrix.
            - calib (torch.Tensor): Calibration for computing matrix_left in
                pinv's matrix.
        const (torch.Tensor): Constants in shape (1 * batch_size * 16 * 2)

    Returns:
        torch.Tensor: Detection results in shape (1 * batch_size * 28),
            containing final bboxes, scores, key points, dimensions, heatmap
            score, yaw angle, position, probability, and classes.
    """
    batch, cat, height, width = heat.size()
    num_joints = kps.shape[1] // 2

    heat = _nms(heat)
    scores, inds, clses, ys, xs = _topk(heat, K=K)

    kps = _permute_and_gather_feat(kps, inds)
    kps = kps.view(batch, K, num_joints * 2)
    kps[..., ::2] += xs.view(batch, K, 1).expand(batch, K, num_joints)
    kps[..., 1::2] += ys.view(batch, K, 1).expand(batch, K, num_joints)
    if reg is not None:
        reg = _permute_and_gather_feat(reg, inds)
        reg = reg.view(batch, K, 2)
        xs = xs.view(batch, K, 1) + reg[:, :, 0:1]
        ys = ys.view(batch, K, 1) + reg[:, :, 1:2]
    else:
        xs = xs.view(batch, K, 1) + 0.5
        ys = ys.view(batch, K, 1) + 0.5
    wh = _permute_and_gather_feat(wh, inds)
    wh = wh.view(batch, K, 2)
    clses = clses.view(batch, K, 1).float()
    scores = scores.view(batch, K, 1)

    bboxes = torch.cat([
        xs - wh[..., 0:1] / 2, ys - wh[..., 1:2] / 2, xs + wh[..., 0:1] / 2,
        ys + wh[..., 1:2] / 2
    ],
                       dim=2)
    dim = _permute_and_gather_feat(dim, inds)
    dim = dim.view(batch, K, 3)

    rot = _permute_and_gather_feat(rot, inds)
    rot = rot.view(batch, K, 8)
    prob = _permute_and_gather_feat(prob, inds)[:, :, 0]
    prob = prob.view(batch, K, 1)
    if kps_heatmap is not None:
        kps_heatmap = _nms(kps_heatmap)
        thresh = 0.1
        kps = kps.view(batch, K, num_joints, 2).permute(0, 2, 1,
                                                        3)  # batch x J x K x 2
        reg_kps = kps.unsqueeze(3).expand(batch, num_joints, K, K, 2)
        # batch x J x K
        hm_score, hm_inds, hm_ys, hm_xs = _topk_channel(kps_heatmap, K=K)
        if kps_offsets is not None:
            kps_offsets = _permute_and_gather_feat(kps_offsets,
                                                   hm_inds.view(batch, -1))
            kps_offsets = kps_offsets.view(batch, num_joints, K, 2)
            hm_xs = hm_xs + kps_offsets[:, :, :, 0]
            hm_ys = hm_ys + kps_offsets[:, :, :, 1]
        else:
            hm_xs = hm_xs + 0.5
            hm_ys = hm_ys + 0.5
        mask = (hm_score > thresh).float()
        hm_score = (1 - mask) * -1 + mask * hm_score
        hm_ys = (1 - mask) * (-10000) + mask * hm_ys
        hm_xs = (1 - mask) * (-10000) + mask * hm_xs
        hm_kps = torch.stack([hm_xs, hm_ys], dim=-1).unsqueeze(2).expand(
            batch, num_joints, K, K, 2)
        dist = (((reg_kps - hm_kps)**2).sum(dim=4)**0.5)
        min_dist, min_ind = dist.min(dim=3)  # batch x J x K
        # batch x J x K x 1
        hm_score = hm_score.gather(2, min_ind).unsqueeze(-1)
        min_dist = min_dist.unsqueeze(-1)
        min_ind = min_ind.view(batch, num_joints, K, 1,
                               1).expand(batch, num_joints, K, 1, 2)
        hm_kps = hm_kps.gather(3, min_ind)
        hm_kps = hm_kps.view(batch, num_joints, K, 2)
        left = bboxes[:, :, 0].view(batch, 1, K, 1)\
            .expand(batch, num_joints, K, 1)
        top = bboxes[:, :, 1].view(batch, 1, K, 1)\
            .expand(batch, num_joints, K, 1)
        right = bboxes[:, :, 2].view(batch, 1, K, 1)\
            .expand(batch, num_joints, K, 1)
        bottom = bboxes[:, :, 3].view(batch, 1, K, 1)\
            .expand(batch, num_joints, K, 1)
        mask = (hm_kps[..., 0:1] < left) + (hm_kps[..., 0:1] > right) + \
               (hm_kps[..., 1:2] < top) + (hm_kps[..., 1:2] > bottom) + \
               (hm_score < thresh) + \
               (min_dist > (torch.max(bottom - top, right - left) * 0.3))
        mask = (mask > 0).float().expand(bottom, num_joints, K, 2)
        kps = (1 - mask) * hm_kps + mask * kps
        kps = kps.permute(0, 2, 1, 3).view(bottom, K, num_joints * 2)
        hm_score = hm_score.permute(0, 2, 1, 3).squeeze(3)
    position, rot_y, kps_inv = transform_img2cam(kps, dim, rot, meta, const)

    detections = torch.cat(
        [bboxes, scores, kps_inv, dim, hm_score, rot_y, position, prob, clses],
        dim=2)

    return detections

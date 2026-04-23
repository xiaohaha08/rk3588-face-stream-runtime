"""YOLOv8 face postprocess adapted from the reference RKNN workflow."""

from __future__ import annotations

import numpy as np


def _filter_boxes(
    boxes: np.ndarray,
    box_confidences: np.ndarray,
    box_class_probs: np.ndarray,
    obj_thresh: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    box_confidences = box_confidences.reshape(-1)
    class_max_score = np.max(box_class_probs, axis=-1)
    classes = np.argmax(box_class_probs, axis=-1)
    pos = np.where(class_max_score * box_confidences >= obj_thresh)
    scores = (class_max_score * box_confidences)[pos]
    return boxes[pos], classes[pos], scores


def _nms_boxes(boxes: np.ndarray, scores: np.ndarray, nms_thresh: float) -> np.ndarray:
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2] - x
    h = boxes[:, 3] - y
    areas = w * h
    order = scores.argsort()[::-1]
    keep: list[int] = []

    while order.size > 0:
        current = int(order[0])
        keep.append(current)
        xx1 = np.maximum(x[current], x[order[1:]])
        yy1 = np.maximum(y[current], y[order[1:]])
        xx2 = np.minimum(x[current] + w[current], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[current] + h[current], y[order[1:]] + h[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1e-5) * np.maximum(0.0, yy2 - yy1 + 1e-5)
        overlap = inter / (areas[current] + areas[order[1:]] - inter)
        order = order[np.where(overlap <= nms_thresh)[0] + 1]
    return np.asarray(keep, dtype=np.int32)


def _dfl(position: np.ndarray) -> np.ndarray:
    batch, channels, height, width = position.shape
    distribution_bins = channels // 4
    values = position.reshape(batch, 4, distribution_bins, height, width)
    exp_values = np.exp(values - np.max(values, axis=2, keepdims=True))
    probs = exp_values / np.sum(exp_values, axis=2, keepdims=True)
    acc = np.arange(distribution_bins, dtype=np.float32).reshape(1, 1, distribution_bins, 1, 1)
    return (probs * acc).sum(2)


def _box_process(position: np.ndarray, img_size: int) -> np.ndarray:
    grid_h, grid_w = position.shape[2:4]
    col, row = np.meshgrid(np.arange(grid_w), np.arange(grid_h))
    col = col.reshape(1, 1, grid_h, grid_w)
    row = row.reshape(1, 1, grid_h, grid_w)
    grid = np.concatenate((col, row), axis=1)
    stride = np.array([img_size // grid_h, img_size // grid_w]).reshape(1, 2, 1, 1)

    pos = _dfl(position)
    box_xy = grid + 0.5 - pos[:, 0:2, :, :]
    box_xy2 = grid + 0.5 + pos[:, 2:4, :, :]
    return np.concatenate((box_xy * stride, box_xy2 * stride), axis=1)


def yolov8_post_process(
    outputs: list[np.ndarray] | tuple[np.ndarray, ...],
    obj_thresh: float = 0.25,
    nms_thresh: float = 0.45,
    img_size: int = 640,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    if not outputs:
        return None, None, None

    boxes_list = []
    scores_list = []
    conf_list = []
    num_branches = 3
    pair = len(outputs) // num_branches

    for branch_index in range(num_branches):
        branch_outputs = np.asarray(outputs[pair * branch_index])
        confidence_outputs = np.asarray(outputs[pair * branch_index + 1])
        boxes_list.append(_box_process(branch_outputs, img_size=img_size))
        conf_list.append(confidence_outputs)
        scores_list.append(np.ones_like(confidence_outputs[:, :1, :, :], dtype=np.float32))

    def _flatten(array: np.ndarray) -> np.ndarray:
        channels = array.shape[1]
        return array.transpose(0, 2, 3, 1).reshape(-1, channels)

    boxes = np.concatenate([_flatten(item) for item in boxes_list])
    classes_conf = np.concatenate([_flatten(item) for item in conf_list])
    scores = np.concatenate([_flatten(item) for item in scores_list])

    boxes, classes, scores = _filter_boxes(boxes, scores, classes_conf, obj_thresh=obj_thresh)
    if boxes.size == 0:
        return None, None, None

    nboxes = []
    nclasses = []
    nscores = []
    for cls in set(classes):
        indices = np.where(classes == cls)
        boxes_for_class = boxes[indices]
        scores_for_class = scores[indices]
        keep = _nms_boxes(boxes_for_class, scores_for_class, nms_thresh=nms_thresh)
        if len(keep) > 0:
            nboxes.append(boxes_for_class[keep])
            nclasses.append(classes[indices][keep])
            nscores.append(scores_for_class[keep])

    if not nclasses:
        return None, None, None
    return np.concatenate(nboxes), np.concatenate(nclasses), np.concatenate(nscores)

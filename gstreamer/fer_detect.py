# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A demo which runs object detection on camera frames using GStreamer.

Run default object detection:
python3 detect.py

Choose different camera and input encoding
python3 detect.py --videosrc /dev/video1 --videofmt jpeg

TEST_DATA=../all_models
Run face detection model:
python3 detect.py \
  --model ${TEST_DATA}/mobilenet_ssd_v2_face_quant_postprocess_edgetpu.tflite

Run coco model:
python3 detect.py \
  --model ${TEST_DATA}/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite \
  --labels ${TEST_DATA}/coco_labels.txt
"""
import argparse
import collections
import common
import gstreamer
import operator
import numpy as np
import os
import re
import svgwrite
import time

#Object = collections.namedtuple('Object', ['id', 'score', 'bbox'])
class Object():
    def __init__(self, id, score, bbox):
        self.id = id
        self.score = score
        self.bbox = bbox

def load_labels(path):
    p = re.compile(r'\s*(\d+)(.+)')
    with open(path, 'r', encoding='utf-8') as f:
       lines = (p.match(line).groups() for line in f.readlines())
       return {int(num): text.strip() for num, text in lines}

def shadow_text(dwg, x, y, text, font_size=20):
    dwg.add(dwg.text(text, insert=(x+1, y+1), fill='black', font_size=font_size))
    dwg.add(dwg.text(text, insert=(x, y), fill='white', font_size=font_size))

def generate_svg(src_size, inference_size, inference_box, objs, labels, text_lines):
    dwg = svgwrite.Drawing('', size=src_size)
    src_w, src_h = src_size
    inf_w, inf_h = inference_size
    box_x, box_y, box_w, box_h = inference_box
    scale_x, scale_y = src_w / box_w, src_h / box_h

    for y, line in enumerate(text_lines, start=1):
        shadow_text(dwg, 10, y*20, line)
    for obj in objs:
        x0, y0, x1, y1 = list(obj.bbox)
        # Relative coordinates.
        x, y, w, h = x0, y0, x1 - x0, y1 - y0
        # Absolute coordinates, input tensor space.
        x, y, w, h = int(x * inf_w), int(y * inf_h), int(w * inf_w), int(h * inf_h)
        # Subtract boxing offset.
        x, y = x - box_x, y - box_y
        # Scale to source coordinate space.
        x, y, w, h = x * scale_x, y * scale_y, w * scale_x, h * scale_y
        percent = int(100 * obj.score)
        label = '{}% {}'.format(percent, labels.get(obj.id, obj.id))
        shadow_text(dwg, x, y - 5, label)
        dwg.add(dwg.rect(insert=(x,y), size=(w, h),
                        fill='none', stroke='red', stroke_width='2'))
    return dwg.tostring()

class BBox(collections.namedtuple('BBox', ['xmin', 'ymin', 'xmax', 'ymax'])):
    """Bounding box.
    Represents a rectangle which sides are either vertical or horizontal, parallel
    to the x or y axis.
    """
    __slots__ = ()

def get_output(interpreter, score_threshold, top_k=1, image_scale=1.0):
    """Returns list of detected objects."""
    boxes = common.output_tensor(interpreter, 0)
    category_ids = common.output_tensor(interpreter, 1)
    scores = common.output_tensor(interpreter, 2)

    def make(i):
        ymin, xmin, ymax, xmax = boxes[i]
        return Object(
            id=int(category_ids[i]),
            score=scores[i],
            bbox=BBox(xmin=np.maximum(0.0, xmin),
                      ymin=np.maximum(0.0, ymin),
                      xmax=np.minimum(1.0, xmax),
                      ymax=np.minimum(1.0, ymax)))
    return [make(i) for i in range(top_k) if scores[i] >= score_threshold]

Category = collections.namedtuple('Category', ['id', 'score'])

def get_output2(interpreter, top_k, score_threshold):
    """Returns no more than top_k categories with score >= score_threshold."""
    scores = common.output_tensor(interpreter, 0)
    categories = [
        Category(i, scores[i])
        for i in np.argpartition(scores, -top_k)[-top_k:]
        if scores[i] >= score_threshold
    ]
    return sorted(categories, key=operator.itemgetter(1), reverse=True)

def main():
    default_model_dir = '../all_models'
    default_model = 'mobilenet_ssd_v2_face_quant_postprocess_edgetpu.tflite'
    default_labels = 'fer_labels.txt'
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', help='.tflite model path',
                        default=os.path.join(default_model_dir,default_model))
    parser.add_argument('--labels', help='label file path',
                        default=os.path.join(default_model_dir, default_labels))
    parser.add_argument('--top_k', type=int, default=1,
                        help='number of categories with highest score to display')
    parser.add_argument('--threshold', type=float, default=0.1,
                        help='classifier score threshold')
    parser.add_argument('--videosrc', help='Which video source to use. ',
                        default='/dev/video0')
    parser.add_argument('--videofmt', help='Input video format.',
                        default='raw',
                        choices=['raw', 'h264', 'jpeg'])
    args = parser.parse_args()

    print('Loading {} with {} labels.'.format(args.model, args.labels))
    interpreter = common.make_interpreter(os.path.join(default_model_dir,default_model))
    interpreter.allocate_tensors()
    # fer interpreter
    interpreter_fer = common.make_interpreter(args.model)
    interpreter_fer.allocate_tensors()
    labels = load_labels(args.labels)

    w, h, _ = common.input_image_size(interpreter)
    inference_size = (w, h)
    # Average fps over last 30 frames.
    fps_counter  = common.avg_fps_counter(30)

    def user_callback(input_tensor, src_size, inference_box):
      nonlocal fps_counter
      start_time = time.monotonic()
      common.set_input(interpreter, input_tensor)
      interpreter.invoke()
      # For larger input image sizes, use the edgetpu.classification.engine for better performance
      objs = get_output(interpreter, args.threshold, args.top_k)
      # Get face detected part
      from PIL import Image
      im = Image.fromarray(common.input_tensor(interpreter))
      src_w, src_h = src_size
      inf_w, inf_h = inference_size
      results = []
      for obj in objs:
        x0, y0, x1, y1 = list(obj.bbox)
        # Relative coordinates.
        x, y, w, h = x0, y0, x1 - x0, y1 - y0
        # Absolute coordinates, input tensor space.
        x, y, w, h = int(x * inf_w), int(y * inf_h), int(w * inf_w), int(h * inf_h)
        crop_rectangle = (x, y, x+w, y+h)
        face_part = im.crop(crop_rectangle)
        # invoke fer interpreter
        common.set_input2(interpreter_fer, face_part)
        interpreter_fer.invoke()
        results = get_output2(interpreter_fer, args.top_k, args.threshold)
        if len(results) > 0:
            setattr(obj, "id", results[0].id)
            setattr(obj, "score", results[0].score)
      
      end_time = time.monotonic()
      text_lines = []
      text_lines = [
          'Inference: {:.2f} ms'.format((end_time - start_time) * 1000),
          'FPS: {} fps'.format(round(next(fps_counter))),
      ]
      for result in results:
          text_lines.append('score={:.2f}: {}'.format(result.score, labels.get(result.id, result.id)))
      print(' '.join(text_lines))
      return generate_svg(src_size, inference_size, inference_box, objs, labels, text_lines)

    result = gstreamer.run_pipeline(user_callback,
                                    src_size=(640, 480),
                                    appsink_size=inference_size,
                                    videosrc=args.videosrc,
                                    videofmt=args.videofmt)

if __name__ == '__main__':
    main()

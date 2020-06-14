
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

"""A demo that runs hand tracking and object detection on camera frames using OpenCV. 2 EDGETPU
"""
import argparse
import collections
import common
import cv2
import numpy as np
import os
import math
from PIL import Image
import re
from edgetpu.detection.engine import DetectionEngine

import time
import svgwrite
import gstreamer
from pose_engine import PoseEngine
import tflite_runtime.interpreter as tflite

Object = collections.namedtuple('Object', ['id', 'score', 'bbox'])

#==============================
EDGES = (
    ('nose', 'left eye'),
    ('nose', 'right eye'),
    ('nose', 'left ear'),
    ('nose', 'right ear'),
    ('left ear', 'left eye'),
    ('right ear', 'right eye'),
    ('left eye', 'right eye'),
    ('left shoulder', 'right shoulder'),
    ('left shoulder', 'left elbow'),
    ('left shoulder', 'left hip'),
    ('right shoulder', 'right elbow'),
    ('right shoulder', 'right hip'),
    ('left elbow', 'left wrist'),
    ('right elbow', 'right wrist'),
    ('left hip', 'right hip'),
    ('left hip', 'left knee'),
    ('right hip', 'right knee'),
    ('left knee', 'left ankle'),
    ('right knee', 'right ankle'),
)
def shadow_text(cv2_im, x, y, text, font_size=16):
    cv2_im = cv2.putText(cv2_im, text, (x + 1, y + 1),
                             cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
    #dwg.add(dwg.text(text, insert=, fill='black',
    #                 font_size=font_size, style='font-family:sans-serif'))
    #dwg.add(dwg.text(text, insert=(x, y), fill='white',
    #                 font_size=font_size, style='font-family:sans-serif'))

def draw_pose(cv2_im, pose, numobject, src_size, color='yellow', threshold=0.2):
    box_x = 0
    box_y = 0  
    box_w = 641
    box_h = 480
    scale_x, scale_y = src_size[0] / box_w, src_size[1] / box_h
    xys = {}
    coor_ave = {}
    totalx = 0
    totaly = 0
    for label, keypoint in pose.keypoints.items():        
        if keypoint.score < threshold: continue
        # Offset and scale to source coordinate space.
        kp_y = int((keypoint.yx[0] - box_y) * scale_y)
        kp_x = int((keypoint.yx[1] - box_x) * scale_x)
        cv2_im = cv2.putText(cv2_im, str(numobject),(kp_x + 1, kp_y + 1), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
        xys[label] = (numobject,kp_x, kp_y)
        totalx += kp_x
        totaly += kp_y
        cv2.circle(cv2_im,(int(kp_x),int(kp_y)),5,(0,255,255),-1)
    #lay vi tri trung binh cua pose.
    coor_ave[numobject] = (totalx/len(xys),totaly/len(xys))
    return xys,coor_ave
    
    '''
    for a, b in EDGES:
        if a not in xys or b not in xys: continue
        anum,ax, ay = xys[a]
        bnum,bx, by = xys[b]
        print(numobject,a,xys[a],b,xys[b])
        cv2.line(cv2_im,(ax, ay), (bx, by),(0,255,255))
    '''
def check_distance(keypoint1,keypoint2):
    dist = math.sqrt((keypoint2[0]-keypoint1[0])**2 + (keypoint2[1]-keypoint1[1])**2)
    return dist

def avg_fps_counter(window_size):
    window = collections.deque(maxlen=window_size)
    prev = time.monotonic()
    yield 0.0  # First fps value.

    while True:
        curr = time.monotonic()
        window.append(curr - prev)
        prev = curr
        yield len(window) / sum(window)


#==============================
def load_labels(path):
    p = re.compile(r'\s*(\d+)(.+)')
    with open(path, 'r', encoding='utf-8') as f:
       lines = (p.match(line).groups() for line in f.readlines())
       return {int(num): text.strip() for num, text in lines}

class BBox(collections.namedtuple('BBox', ['xmin', 'ymin', 'xmax', 'ymax'])):
    """Bounding box.
    Represents a rectangle which sides are either vertical or horizontal, parallel
    to the x or y axis.
    """
    __slots__ = ()

def get_output(interpreter, score_threshold, top_k, image_scale=1.0):
    """Returns list of detected objects."""
    boxes = common.output_tensor(interpreter, 0)
    class_ids = common.output_tensor(interpreter, 1)
    scores = common.output_tensor(interpreter, 2)
    count = int(common.output_tensor(interpreter, 3))

    def make(i):
        ymin, xmin, ymax, xmax = boxes[i]
        return Object(
            id=int(class_ids[i]),
            score=scores[i],
            bbox=BBox(xmin=np.maximum(0.0, xmin),
                      ymin=np.maximum(0.0, ymin),
                      xmax=np.minimum(1.0, xmax),
                      ymax=np.minimum(1.0, ymax)))

    return [make(i) for i in range(top_k) if scores[i] >= score_threshold]

def main():
    default_model_dir = '../all_models'
    default_model = 'posenet/posenet_mobilenet_v1_075_481_641_quant_decoder_edgetpu.tflite'
    default_labels = 'hand_label.txt'
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', help='.tflite model path',
                        default=os.path.join(default_model_dir,default_model))
    parser.add_argument('--labels', help='label file path',
                        default=os.path.join(default_model_dir, default_labels))
    parser.add_argument('--top_k', type=int, default=1,
                        help='number of categories with highest score to display')
    parser.add_argument('--camera_idx', type=str, help='Index of which video source to use. ', default = 0)
    parser.add_argument('--threshold', type=float, default=0.5,
                        help='classifier score threshold')
    args = parser.parse_args()

    #print('Loading Handtracking model {} with {} labels.'.format(args.model, args.labels))

    #engine = DetectionEngine(args.model)
    #labels = load_labels(args.labels)
    #=====================================================================
    src_size = (640, 480)
    print('Loading Pose model {}'.format(args.model))
    engine = PoseEngine(args.model)
    #engine = PoseEngine('models/mobilenet/posenet_mobilenet_v1_075_481_641_quant_decoder_edgetpu.tflite')
    #=====================================================================
    # for detection
    print('Loading Detection model {} with {} labels.'.format('../all_models/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite', '../all_models/coco_labels.txt'))
    #interpreter2 = common.make_interpreter('../all_models/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite')
    #interpreter2.allocate_tensors()
    engine2 = DetectionEngine('../all_models/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite')
    labels2 = load_labels('../all_models/coco_labels.txt')

    cap = cv2.VideoCapture(args.camera_idx)

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        cv2_im = frame

        cv2_im_rgb = cv2.cvtColor(cv2_im, cv2.COLOR_BGR2RGB)
        pil_im = Image.fromarray(cv2_im_rgb)

        #======================================pose processing=================================
        
        poses, inference_time = engine.DetectPosesInImage(np.uint8(pil_im.resize((641, 481), Image.NEAREST)))
        #print('Posese is',poses)
    
        n = 0
        sum_process_time = 0
        sum_inference_time = 0
        ctr = 0
        fps_counter  = avg_fps_counter(30)
        
        input_shape = engine.get_input_tensor_shape()

        inference_size = (input_shape[2], input_shape[1])


        #print('Shape is',input_shape)
        #print('inference size is:',inference_size)
        start_time = time.monotonic()
        
        end_time = time.monotonic()
        n += 1
        sum_process_time += 1000 * (end_time - start_time)
        sum_inference_time += inference_time

        avg_inference_time = sum_inference_time / n
        text_line = 'PoseNet: %.1fms (%.2f fps) TrueFPS: %.2f' % (
            avg_inference_time, 1000 / avg_inference_time, next(fps_counter)
        )
        
        shadow_text(cv2_im, 10, 20, text_line)
        numobject = 0
        xys={}
        coor_ave={}
        #draw_pose(cv2_im, poses, dis, src_size)
        for pose in poses:
            '''
        for i in range(len(poses)-1):
            pose = poses[i]
            
            #print(pose.keypoints.items())
            for label, keypoint in pose.keypoints.items():
                #print(label)
                #print(keypoint)
                if keypoint.score < 0.2: continue
                if (label=='nose'):
                    print('yx0,',keypoint.yx)
                    
            for j in range(len(poses)):
                pose1 = poses[j]
                #print(pose.keypoints.items())
                for label, keypoint in pose1.keypoints.items():
                    if keypoint.score < 0.2: continue
                    if (label=='nose'):
                        print('yx1,',keypoint.yx)    
            '''
            
            xys,coor_ave=draw_pose(cv2_im, pose, numobject, src_size)
            numobject += 1
            #print('len coor_av',coor_ave)
            #print(xys,coor_ave)kghkkgkgkgerg.hbjbbsbdbs
        
        for a, b in EDGES:
            if a not in xys or b not in xys: continue
            anum,ax, ay = xys[a]
            bnum,bx, by = xys[b]
            #print(numobject,a,xys[a],b,xys[b])
            cv2.line(cv2_im,(ax, ay), (bx, by),(0,255,255))
        a = []
        b = []
        #leng = coor_ave.length
        #print(leng)
        '''
        print('len coor',len(coor_ave))
        for i in range(0,len(coor_ave)):
            a=coor_ave[i]
            print('aaaa',a)
            for j in range(i+1,len(coor_ave)):
                if(i==j):
                    break
                else:
                    b=coor_ave[j]
                    print('bbbb')
                    print(b)
                    '''
        #==============================================================================================    
        #cv2_im = append_objs_to_img(cv2_im, objs, labels)

        # detection
        #common.set_input(interpreter2, pil_im)
        #interpreter2.invoke()
        #objs = get_output(interpreter2, score_threshold=0.2, top_k=3)
        objs = engine2.detect_with_image(pil_im,
                                  threshold=0.2,
                                  keep_aspect_ratio=True,
                                  relative_coord=True,
                                  top_k=3)
                                

        cv2_im = append_objs_to_img(cv2_im, objs, labels2)

        cv2.imshow('frame', cv2_im)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

def append_objs_to_img(cv2_im, objs, labels):
    ''' Original Code
    height, width, channels = cv2_im.shape
    for obj in objs:
        x0, y0, x1, y1 = obj.bounding_box.flatten().tolist() #list(obj.bbox)
        x0, y0, x1, y1 = int(x0*width), int(y0*height), int(x1*width), int(y1*height)
        percent = int(100 * obj.score)
        label = '{}% {}'.format(percent, labels.get(obj.label_id, obj.label_id))

        cv2_im = cv2.rectangle(cv2_im, (x0, y0), (x1, y1), (0, 255, 0), 2)
        cv2_im = cv2.putText(cv2_im, label, (x0, y0+30),
                             cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
    return cv2_im
    '''
    height, width, channels = cv2_im.shape
    classIDs = []
    box_obj = []
    confidences = []
    for obj in objs:

        x0, y0, x1, y1 = obj.bounding_box.flatten().tolist() #list(obj.bbox)
        x0, y0, x1, y1 = int(x0*width), int(y0*height), int(x1*width), int(y1*height)
        percent = int(100 * obj.score)

        label = '{}% {}'.format(percent, labels.get(obj.label_id, obj.label_id))
        if(labels.get(obj.label_id, obj.label_id)=='person'):
            
            cv2_im = cv2.rectangle(cv2_im, (x0, y0), (x1, y1), (0, 255, 0), 2)
            cv2_im = cv2.putText(cv2_im, label, (x0, y0+30),
                             cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
    return cv2_im


if __name__ == '__main__':
    main()
"""The same calibrated colour and silhouette kernels used by offline bundles."""
import time
import cv2
from concurrent.futures import ThreadPoolExecutor
from beanoflight.detection import DetectorSettings
from beanoflight.statistics_features import foreground_mask, component_crop_mask, extract_view_features, local_area_scale, paired_features


class Measurements:
    def __init__(self, source, background, calibration, crop_size=320):
        self.source=source; self.background=background; self.calibration=calibration;self.crop_size=crop_size
        self.left_settings=DetectorSettings().updated(close_kernel=3)
        self.right_settings=self.left_settings.updated(blur_kernel=5,threshold=22,close_iterations=1,
            open_kernel=3,open_iterations=1,dilate_iterations=0)
        self.right_background=source.right_background_gray()
        self.fallback_background=source.right_background_gray(dual_green=True)
        self.frame_index=None;self.foregrounds={}
        self.right_worker=ThreadPoolExecutor(1,thread_name_prefix='right-colour')

    def close(self):
        self.right_worker.shutdown(wait=True)

    def measure(self, frame, centroid, index, timestamp):
        started=time.perf_counter_ns(); source=self.source
        if self.frame_index!=index: self.foregrounds.clear();self.frame_index=index
        prepared=source.prepare_stereo_crop(frame,centroid,self.crop_size,allow_padding=False,allow_resize=True)
        if prepared is None: return None,'stereo_crop_unavailable'
        if 'left' not in self.foregrounds:
            self.foregrounds['left']=foreground_mask(frame.detection_gray,self.background,self.left_settings)
            self.foregrounds['right']=foreground_mask(source.right_detection_gray(frame),self.right_background,self.right_settings)
        left_mask=component_crop_mask(self.foregrounds['left'],prepared.pair.caml_centroid_px,prepared.source_size_px,maximum_distance_px=24.)
        right_mask=component_crop_mask(self.foregrounds['right'],prepared.pair.camr_centroid_px,prepared.source_size_px,maximum_distance_px=24.)
        if right_mask is None:
            if 'fallback' not in self.foregrounds:
                self.foregrounds['fallback']=foreground_mask(source.right_detection_gray(frame,dual_green=True),self.fallback_background,self.right_settings)
            right_mask=component_crop_mask(self.foregrounds['fallback'],prepared.pair.camr_centroid_px,prepared.source_size_px,maximum_distance_px=24.)
        if left_mask is None or right_mask is None: return None,'component_mask_unavailable'
        def left_mm(p): return self.calibration.pixel_to_mm(source.undistort_point(p))
        def right_mm(p): return self.calibration.pixel_to_mm(source.stereo_calibration.project_distorted_camr_to_undistorted_caml(p))
        def measure_right():
            image=prepared.camr_materializer()
            features=extract_view_features(image,right_mask,area_scale_mm2_per_px=local_area_scale(prepared.pair.camr_centroid_px,right_mm))
            return image,features
        right_future=self.right_worker.submit(measure_right)
        try:
            left=prepared.caml_materializer()
            lf=extract_view_features(left,left_mask,area_scale_mm2_per_px=local_area_scale(prepared.pair.caml_centroid_px,left_mm))
            right,rf=right_future.result()
        except ValueError:
            try:right_future.result()
            except ValueError:pass
            return None,'invalid_or_clipped_silhouette'
        finally:
            # A worker must release its RAW-frame view before the caller releases it.
            if not right_future.done():
                try:right_future.result()
                except Exception:pass
        row={'frame_index':index,'timestamp_ns':timestamp,'right_frame_index':prepared.pair.right_frame_index,
            'synchronization_delta_ns':prepared.pair.synchronization_delta_ns,'source_crop_size_px':prepared.source_size_px,
            'caml_centroid_x_px':prepared.pair.caml_centroid_px[0],'caml_centroid_y_px':prepared.pair.caml_centroid_px[1],
            'camr_centroid_x_px':prepared.pair.camr_centroid_px[0],'camr_centroid_y_px':prepared.pair.camr_centroid_px[1]}
        row.update({'caml_'+k:v for k,v in lf.values.items()});row.update({'camr_'+k:v for k,v in rf.values.items()})
        row.update(paired_features(lf.values,rf.values,prepared.pair.refinement_distance_px))
        row['measurement_ms']=(time.perf_counter_ns()-started)/1e6
        return (row,left,right),None

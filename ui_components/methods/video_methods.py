import os
import random
import string
import tempfile
import time
from typing import List
import ffmpeg
import streamlit as st
import uuid
from moviepy.editor import concatenate_videoclips, TextClip, VideoFileClip, vfx, AudioFileClip
from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip

from backend.models import InternalFileObject
from shared.constants import AnimationToolType, InferenceType, InternalFileTag
from shared.file_upload.s3 import is_s3_image_url
from ui_components.constants import VideoQuality
from ui_components.methods.file_methods import convert_bytes_to_file, generate_temp_file
from ui_components.models import InternalFrameTimingObject, InternalSettingObject, InternalShotObject
from utils.data_repo.data_repo import DataRepo
from utils.media_processor.interpolator import VideoInterpolator
from utils.media_processor.video import VideoProcessor


# NOTE: interpolated_clip_uuid signals which clip to promote to timed clip (this is the main variant)
# this function returns the 'single' preview_clip, which is basically timed_clip with the frame number
def create_or_get_single_preview_video(timing_uuid, interpolated_clip_uuid=None):
    from ui_components.methods.file_methods import generate_temp_file
    from ui_components.methods.common_methods import get_audio_bytes_for_slice
    from ui_components.methods.common_methods import process_inference_output
    from shared.constants import QUEUE_INFERENCE_QUERIES

    data_repo = DataRepo()

    timing: InternalFrameTimingObject = data_repo.get_timing_from_uuid(
        timing_uuid)
    project_details: InternalSettingObject = data_repo.get_project_setting(
        timing.project.uuid)

    if not len(timing.interpolated_clip_list):
        timing.interpolation_steps = 3
        next_timing = data_repo.get_next_timing(timing.uuid)
        img_list = [timing.source_image.location, next_timing.source_image.location]
        res = VideoInterpolator.video_through_frame_interpolation(img_list, \
                                                                  {"interpolation_steps": timing.interpolation_steps}, 1, \
                                                                    False)      # TODO: queuing is not enabled here
        
        output_url, log = res[0]

        inference_data = {
            "inference_type": InferenceType.SINGLE_PREVIEW_VIDEO.value,
            "file_location_to_save": "videos/" + timing.project.uuid + "/assets/videos" + (str(uuid.uuid4())) + ".mp4",
            "mime_type": "video/mp4",
            "output": output_url,
            "project_uuid": timing.project.uuid,
            "log_uuid": log.uuid,
            "timing_uuid": timing_uuid
        }

        process_inference_output(**inference_data)
        
    timing = data_repo.get_timing_from_uuid(timing_uuid)
    if not timing.timed_clip:
        interpolated_clip = data_repo.get_file_from_uuid(interpolated_clip_uuid) if interpolated_clip_uuid \
                                else timing.interpolated_clip_list[0]
        
        output_video = update_speed_of_video_clip(interpolated_clip, timing_uuid)
        data_repo.update_specific_timing(timing_uuid, timed_clip_id=output_video.uuid)

    if not timing.preview_video:
        timing = data_repo.get_timing_from_uuid(timing_uuid)
        timed_clip = timing.timed_clip
        
        temp_video_file = None
        if timed_clip.hosted_url and is_s3_image_url(timed_clip.hosted_url):
            temp_video_file = generate_temp_file(timed_clip.hosted_url, '.mp4')

        file_path = temp_video_file.name if temp_video_file else timed_clip.local_path
        clip = VideoFileClip(file_path)
        
        if temp_video_file:
            os.remove(temp_video_file.name)

        number_text = TextClip(str(timing.aux_frame_index),
                               fontsize=24, color='white')
        number_background = TextClip(" ", fontsize=24, color='black', bg_color='black', size=(
            number_text.w + 10, number_text.h + 10))
        number_background = number_background.set_position(
            ('left', 'top')).set_duration(clip.duration)
        number_text = number_text.set_position(
            (number_background.w - number_text.w - 5, number_background.h - number_text.h - 5)).set_duration(clip.duration)
        clip_with_number = CompositeVideoClip([clip, number_background, number_text])

        temp_output_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", mode='wb')
        clip_with_number.write_videofile(filename=temp_output_file.name, codec='libx264', audio_codec='aac')

        if temp_output_file:
            video_bytes = None
            with open(file_path, 'rb') as f:
                video_bytes = f.read()

            preview_video = convert_bytes_to_file(
                file_location_to_save="videos/" + str(timing.project.uuid) + "/assets/videos/0_raw/" + str(uuid.uuid4()) + ".png",
                mime_type="video/mp4",
                file_bytes=video_bytes,
                project_uuid=timing.project.uuid,
                inference_log_id=None
            )

            data_repo.update_specific_timing(timing_uuid, preview_video_id=preview_video.uuid)
            os.remove(temp_output_file.name)

    # adding audio if the audio file is present
    if project_details.audio:
        audio_bytes = get_audio_bytes_for_slice(timing_uuid)
        add_audio_to_video_slice(timing.preview_video, audio_bytes)

    timing: InternalFrameTimingObject = data_repo.get_timing_from_uuid(
        timing_uuid)
    return timing.preview_video

def create_single_interpolated_clip(timing_uuid, quality, settings={}, variant_count=1):
    '''
    - this includes all the animation styles [direct morphing, interpolation, image to video]
    - this stores the newly created video in the interpolated_clip_list and promotes them to
    timed_clip (if it's not already present)
    '''

    from ui_components.methods.common_methods import process_inference_output
    from shared.constants import QUEUE_INFERENCE_QUERIES

    data_repo = DataRepo()
    timing: InternalFrameTimingObject = data_repo.get_timing_from_uuid(timing_uuid)
    next_timing: InternalFrameTimingObject = data_repo.get_next_timing(timing_uuid)
    prev_timing: InternalFrameTimingObject = data_repo.get_prev_timing(timing_uuid)

    if not next_timing:
        st.error('This is the last image. Please select images having both prev & next images')
        time.sleep(0.5)
        return None
    
    if not prev_timing:
        st.error('This is the first image. Please select images having both prev & next images')
        time.sleep(0.5)
        return None

    if quality == 'full':
        interpolation_steps = VideoInterpolator.calculate_dynamic_interpolations_steps(timing.clip_duration)
    elif quality == 'preview':
        interpolation_steps = 3

    timing.interpolated_steps = interpolation_steps
    img_list = [prev_timing.primary_image.location, timing.primary_image.location, next_timing.primary_image.location]
    settings.update(interpolation_steps=timing.interpolation_steps)

    # res is an array of tuples (video_bytes, log)
    res = VideoInterpolator.create_interpolated_clip(
        img_list,
        timing.animation_style,
        settings,
        variant_count,
        QUEUE_INFERENCE_QUERIES
    )

    for (output, log) in res:
        inference_data = {
            "inference_type": InferenceType.FRAME_INTERPOLATION.value,
            "output": output,
            "log_uuid": log.uuid,
            "settings": settings,
            "timing_uuid": timing_uuid
        }

        process_inference_output(**inference_data)

def update_speed_of_video_clip(video_file: InternalFileObject, timing_uuid) -> InternalFileObject:
    from ui_components.methods.file_methods import generate_temp_file, convert_bytes_to_file

    data_repo = DataRepo()

    timing: InternalFrameTimingObject = data_repo.get_timing_from_uuid(
        timing_uuid)

    desired_duration = timing.clip_duration
    animation_style = timing.animation_style

    temp_video_file = None
    if video_file.hosted_url and is_s3_image_url(video_file.hosted_url):
        temp_video_file = generate_temp_file(video_file.hosted_url, '.mp4')
    
    location_of_video = temp_video_file.name if temp_video_file else video_file.local_path
    
    new_file_name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16)) + ".mp4"
    new_file_location = "videos/" + str(timing.project.uuid) + "/assets/videos/1_final/" + str(new_file_name)

    video_bytes = VideoProcessor.update_video_speed(
        location_of_video,
        animation_style,
        desired_duration
    )

    video_file = convert_bytes_to_file(
        new_file_location,
        "video/mp4",
        video_bytes,
        timing.project.uuid
    )

    if temp_video_file:
        os.remove(temp_video_file.name)

    return video_file

def add_audio_to_video_slice(video_file, audio_bytes):
    video_location = video_file.local_path
    # Save the audio bytes to a temporary file
    audio_file = "temp_audio.wav"
    with open(audio_file, 'wb') as f:
        f.write(audio_bytes.getvalue())

    # Create an input video stream
    video_stream = ffmpeg.input(video_location)

    # Create an input audio stream
    audio_stream = ffmpeg.input(audio_file)

    # Add the audio stream to the video stream
    output_stream = ffmpeg.output(video_stream, audio_stream, "output_with_audio.mp4",
                                  vcodec='copy', acodec='aac', strict='experimental')

    # Run the ffmpeg command
    output_stream.run()

    # Remove the original video file and the temporary audio file
    os.remove(video_location)
    os.remove(audio_file)

    # TODO: handle online update in this case
    # Rename the output file to have the same name as the original video file
    os.rename("output_with_audio.mp4", video_location)


def render_video(final_video_name, project_uuid, file_tag=InternalFileTag.GENERATED_VIDEO.value):
    '''
    combines the main variant of all the shots to form the final video. no processing happens in this, only
    simple combination
    '''
    from ui_components.methods.file_methods import convert_bytes_to_file, generate_temp_file

    data_repo = DataRepo()

    if not final_video_name:
        st.error("Please enter a video name")
        time.sleep(0.3)
        return

    video_list = []
    temp_file_list = []

    # combining all the main_clip of shots in finalclip, and keeping track of temp video files
    # in temp_file_list
    shot_list: List[InternalShotObject] = data_repo.get_shot_list_from_project(project_uuid)
    for shot in shot_list:
        if not shot.main_clip:
            st.error("Please generate all videos")
            time.sleep(0.3)
            return
        
        temp_video_file = None
        if shot.main_clip.hosted_url:
            temp_video_file = generate_temp_file(shot.main_clip.hosted_url, '.mp4')
            temp_file_list.append(temp_video_file)

        file_path = temp_video_file.name if temp_video_file else shot.main_clip.local_path
        video_list.append(file_path)

    finalclip = concatenate_videoclips([VideoFileClip(v) for v in video_list])

    # attaching audio to finalclip
    project_settings = data_repo.get_project_settings_from_uuid(project_uuid)
    output_video_file = f"videos/{project_uuid}/assets/videos/2_completed/{final_video_name}.mp4"
    if project_settings.audio:
        temp_audio_file = None
        if 'http' in project_settings.audio.location:
            temp_audio_file = generate_temp_file(project_settings.audio.location, '.mp4')
            temp_file_list.append(temp_audio_file)

        audio_location = temp_audio_file.name if temp_audio_file else project_settings.audio.location
        
        audio_clip = AudioFileClip(audio_location)
        finalclip = finalclip.set_audio(audio_clip)

    # writing the video to the temp file
    temp_video_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    finalclip.write_videofile(
        temp_video_file.name,
        fps=60,  # or 60 if your original video is 60fps
        audio_bitrate="128k",
        bitrate="5000k",
        codec="libx264",
        audio_codec="aac"
    )

    temp_video_file.close()
    video_bytes = None
    with open(temp_video_file.name, "rb") as f:
        video_bytes = f.read()

    _ = convert_bytes_to_file(
        file_location_to_save=output_video_file,
        mime_type="video/mp4",
        file_bytes=video_bytes,
        project_uuid=project_uuid,
        inference_log_id=None,
        filename=final_video_name,
        tag=file_tag
    )

    for file in temp_file_list:
        os.remove(file.name)

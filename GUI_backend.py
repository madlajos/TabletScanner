from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from tkinter import filedialog, Tk
import os
import cv2
import logging
from logging.handlers import RotatingFileHandler
from globals import cameras, stream_running, stream_threads, grab_locks, turntable_position
from pypylon import pylon
from cameracontrol import (apply_camera_settings, set_centered_offset, 
                           validate_and_set_camera_param, get_camera_properties, Handler)
import porthandler
import imageprocessing
from settings_manager import load_settings, save_settings, get_settings

app = Flask(__name__)
app.secret_key = 'Zoltek'
logging.basicConfig(level=logging.DEBUG)
CORS(app)
app.debug = True

file_handler = RotatingFileHandler('flask.log', maxBytes=10240, backupCount=10)
app.logger.addHandler(file_handler)
console_handler = logging.StreamHandler()
app.logger.addHandler(console_handler)
app.logger.setLevel(logging.DEBUG)

# Might need to be removed
camera_properties = {'main': None, 'side': None}

MAIN_CAMERA_ID = '40569959'
SIDE_CAMERA_ID = '40569958'

CAMERA_IDS = {
    'main': MAIN_CAMERA_ID,
    'side': SIDE_CAMERA_ID
}


### Serial Device Functions ###
# Define the route for checking device status
@app.route('/api/connect-to-<device_name>', methods=['POST'])
def connect_serial_device(device_name):
    try:
        app.logger.info(f"Attempting to connect to {device_name}")
        device = None
        if device_name == 'turntable':
            device = porthandler.connect_to_turntable()
            app.logger.debug(f"Turntable connection attempt result: {device}")
        else:
            app.logger.error(f"Invalid device name: {device_name}")
            return jsonify({'error': 'Invalid device name'}), 400

        if device is not None:
            # Update global state
            if device_name == 'turntable':
                porthandler.turntable = device

            app.logger.info(f"Successfully connected to {device_name}")
            return jsonify('ok')
        else:
            app.logger.error(f"Failed to connect to {device_name}: No COM ports or matching device not found")
            return jsonify({'error': f'Failed to connect to {device_name}. No COM ports available or matching device not found'}), 404
    except Exception as e:
        app.logger.exception(f"Exception occurred while connecting to {device_name}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/disconnect-to-<device_name>', methods=['POST'])
def disconnect_serial_device(device_name):
    try:
        app.logger.info(f"Attempting to disconnect from {device_name}")
        porthandler.disconnect_serial_device(device_name)
        app.logger.info(f"Successfully disconnected from {device_name}")
        return jsonify('ok')
    except Exception as e:
        logging.exception(f"Exception occurred while disconnecting from {device_name}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/check-connections', methods=['GET'])
def check_serial_connections():
    turntable_connected = porthandler.turntable is not None
    return jsonify({
        'turntableConnected': turntable_connected
    })
    
### Turntable Functions ###
@app.route('/home_turntable_with_image', methods=['POST'])
def home_turntable_with_image():
    try:
        app.logger.info("Homing process initiated.")
        camera_type = 'main'

        with grab_locks[camera_type]:
            camera = cameras.get(camera_type)

            if camera is None or not camera.IsOpen():
                app.logger.error("Main camera is not connected or open.")
                return jsonify({"error": "Main camera is not connected or open."}), 400

            # Grab a single image without stopping the stream
            grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

            if grab_result.GrabSucceeded():
                app.logger.info("Image grabbed successfully.")
                image = grab_result.Array
                grab_result.Release()

                # Process the image
                rotation_needed = imageprocessing.home_turntable_with_image(image)
                command = f"{abs(rotation_needed)},{1 if rotation_needed > 0 else 0}"
                
                app.logger.info(f"Image processing complete. Rotation needed: {rotation_needed}")

                # Send rotation command to the turntable
                porthandler.write_turntable(command)
                app.logger.info("Rotation command sent to turntable.")

                # Set position to 0 after homing
                globals.turntable_position = 0  
                app.logger.info("Homing completed successfully.")

                return jsonify({
                    "message": "Homing successful",
                    "rotation": rotation_needed,
                    "current_position": globals.turntable_position
                })
            else:
                grab_result.Release()
                app.logger.error("Failed to grab image from camera.")
                return jsonify({"error": "Failed to grab image from camera."}), 500

    except Exception as e:
        app.logger.error(f"Exception during homing: {str(e)}")
        return jsonify({"error": str(e)}), 500

    
@app.route('/move_turntable_relative', methods=['POST'])
def move_turntable_relative():
    global turntable_position
    data = request.get_json()
    move_by = data.get('degrees')

    if move_by is None or not isinstance(move_by, (int, float)):
        return jsonify({'error': 'Invalid input, provide degrees as a number'}), 400

    direction = 'CW' if move_by > 0 else 'CCW'
    command = f"{abs(move_by)},{1 if move_by > 0 else 0}"

    try:
        porthandler.write_turntable(command)

        if turntable_position is not None:
            turntable_position = (turntable_position + move_by) % 360  # Update position only if homed
        
        return jsonify({
            'message': f'Turntable moved {move_by} degrees {direction}',
            'current_position': turntable_position if turntable_position is not None else '?'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/move_turntable_absolute', methods=['POST'])
def move_turntable_absolute():
    global turntable_position
    data = request.get_json()
    target_position = data.get('degrees')

    if target_position is None or not isinstance(target_position, (int, float)):
        return jsonify({'error': 'Invalid input, provide degrees as a number'}), 400

    # Calculate the shortest path to target position
    move_by = (target_position - turntable_position) % 360
    if move_by > 180:
        move_by -= 360  # Take the shorter path

    direction = 1 if move_by > 0 else 0
    command = f"{abs(move_by)},{direction}"

    try:
        # Send command to Arduino
        porthandler.write_turntable(command)

        #TODO: Wait for confirmation from Arduino that the rotation was successful
        # Update the global position
        turntable_position = target_position % 360

        return jsonify({'message': f'Turntable moved to {target_position} degrees {direction}',
                        'current_position': turntable_position})
    except Exception as e:
        return jsonify({'error': str(e)}), 500













# Define the route for starting the video stream
@app.route('/select-folder', methods=['GET'])
def select_folder():
    try:
        root = Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder_selected = filedialog.askdirectory()
        root.destroy()
        if folder_selected:
            return jsonify({'folder': folder_selected}), 200
        else:
            return jsonify({'error': 'No folder selected'}), 400
    except Exception as e:
        app.logger.exception("Failed to select folder")
        return jsonify({'error': str(e)}), 500

@app.route('/start-video-stream', methods=['GET'])
def start_video_stream():
    camera_type = request.args.get('type')
    scale_factor = float(request.args.get('scale', 0.25))

    # Check if stream is already running
    if stream_running.get(camera_type, False):
        app.logger.info(f"{camera_type.capitalize()} stream is already running.")
        return Response(generate_frames(camera_type, scale_factor),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    # Start the camera stream if not already running
    result = start_camera_stream_internal(camera_type, scale_factor)
    
    if "error" in result:
        return jsonify(result), 400


@app.route('/stop-video-stream', methods=['POST'])
def stop_video_stream():
    camera_type = request.args.get('type')
    app.logger.info(f"Received stop request for {camera_type}")

    try:
        message = stop_camera_stream(camera_type)
        return jsonify({"message": message}), 200
    except ValueError as ve:
        # E.g., invalid camera type
        app.logger.error(str(ve))
        return jsonify({"error": str(ve)}), 400
    except RuntimeError as re:
        app.logger.error(str(re))
        return jsonify({"error": str(re)}), 500
    except Exception as e:
        app.logger.exception(f"Unexpected exception while stopping {camera_type} stream.")
        return jsonify({"error": str(e)}), 500

def generate_frames(camera_type, scale_factor=0.25):
    app.logger.info(f"Generating frames for {camera_type} with scale factor {scale_factor}")
    camera = cameras.get(camera_type)

    if not camera:
        app.logger.error(f"{camera_type.capitalize()} camera is not connected.")
        return

    if not camera.IsGrabbing():
        app.logger.info(f"{camera_type.capitalize()} camera starting grabbing.")
        camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    try:
        while stream_running[camera_type]:
            with grab_locks[camera_type]:
                grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

                if grab_result.GrabSucceeded():
                    image = grab_result.Array
                    
                    image = cv2.flip(image, 1)
                    image = cv2.flip(image, 0)
                    if scale_factor != 1.0:
                        width = int(image.shape[1] * scale_factor)
                        height = int(image.shape[0] * scale_factor)
                        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

                    _, frame = cv2.imencode('.jpg', image)
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame.tobytes() + b'\r\n')
                
                grab_result.Release()
    except Exception as e:
        app.logger.error(f"Error in {camera_type} video stream: {e}")
    finally:
        stream_running[camera_type] = False
        app.logger.info(f"{camera_type.capitalize()} camera streaming thread stopped.")


@app.route('/connect-camera', methods=['POST'])
def connect_camera():
    camera_type = request.args.get('type')
    if camera_type not in CAMERA_IDS:
        return jsonify({"error": "Invalid camera type specified"}), 400

    result = connect_camera_internal(camera_type)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result), 200

@app.route('/disconnect-camera', methods=['POST'])
def disconnect_camera():
    camera_type = request.args.get('type')

    if camera_type not in cameras or cameras[camera_type] is None:
        app.logger.warning(f"{camera_type.capitalize()} camera is already disconnected or not initialized")
        return jsonify({"status": "already disconnected"}), 200

    try:
        stop_camera_stream(camera_type)
        app.logger.info(f"{camera_type.capitalize()} stream stopped before disconnecting.")
    except ValueError:
        app.logger.warning(f"Failed to stop {camera_type} stream: Invalid camera type.")
        # Decide how you want to handle this. If invalid camera type is fatal, return here:
        return jsonify({"error": "Invalid camera type"}), 400
    except RuntimeError as re:
        app.logger.warning(f"Error stopping {camera_type} stream: {str(re)}")
        # Maybe we continue to shut down the camera anyway
    except Exception as e:
        app.logger.error(f"Failed to disconnect {camera_type} camera: {e}")
        return jsonify({"error": str(e)}), 500

    camera = cameras.get(camera_type, None)
    if camera and camera.IsGrabbing():
        camera.StopGrabbing()
        app.logger.info(f"{camera_type.capitalize()} camera grabbing stopped.")

    if camera and camera.IsOpen():
        camera.Close()
        app.logger.info(f"{camera_type.capitalize()} camera closed.")

    # Clean up references
    cameras[camera_type] = None
    camera_properties[camera_type] = None  # Make sure camera_properties is in scope
    app.logger.info(f"{camera_type.capitalize()} camera disconnected successfully.")

    return jsonify({"status": "disconnected"}), 200

    
@app.route('/api/status/camera', methods=['GET'])
def get_camera_status():
    camera_type = request.args.get('type')

    if camera_type not in cameras:
        app.logger.error(f"Invalid camera type: {camera_type}")
        return jsonify({"error": "Invalid camera type specified"}), 400

    camera = cameras.get(camera_type)
    is_connected = camera is not None and camera.IsOpen()
    is_streaming = stream_running.get(camera_type, False)

    return jsonify({
        "connected": is_connected,
        "streaming": is_streaming
    }), 200
    
@app.route('/api/status/serial/<device_name>', methods=['GET'])
def get_serial_device_status(device_name):
    logging.debug(f"Received status request for device: {device_name}")
    device = None
    if device_name == 'turntable':
        device = porthandler.turntable
    else:
        logging.error("Invalid device name")
        return jsonify({'error': 'Invalid device name'}), 400

    if device is not None:
        logging.debug(f"{device_name} is connected on port {device.port}")
        return jsonify({'connected': True, 'port': device.port})
    else:
        logging.debug(f"{device_name} is not connected")
        return jsonify({'connected': False})

@app.route('/api/update-camera-settings', methods=['POST'])
def update_camera_settings():
    try:
        data = request.json
        camera_type = data.get('camera_type')
        setting_name = data.get('setting_name')
        setting_value = data.get('setting_value')

        app.logger.info(f"Updating {camera_type} camera setting: {setting_name} = {setting_value}")

        # Apply the setting to the camera
        updated_value = validate_and_set_camera_param(
            cameras[camera_type],
            setting_name,
            setting_value,
            camera_properties[camera_type],
            camera_type
        )

        settings_data = get_settings()
        settings_data['camera_params'][camera_type][setting_name] = updated_value
        save_settings()

        app.logger.info(f"{camera_type.capitalize()} camera setting {setting_name} updated and saved to settings.json")

        return jsonify({
            "message": f"{camera_type.capitalize()} camera {setting_name} updated and saved.",
            "updated_value": updated_value
        }), 200

    except Exception as e:
        app.logger.exception("Failed to update camera settings")
        return jsonify({"error": str(e)}), 500

@app.route('/video-stream', methods=['GET'])
def video_stream():
    camera_type = request.args.get('type')

    if camera_type not in cameras:
        app.logger.error(f"Invalid camera type: {camera_type}")
        return jsonify({"error": "Invalid camera type specified"}), 400

    app.logger.info(f"{camera_type.capitalize()} camera stream started successfully")
    return Response(generate_frames(camera_type), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/get-camera-settings', methods=['GET'])
def get_camera_settings():
    camera_type = request.args.get('type')
    app.logger.info(f"API Call: /api/get-camera-settings for {camera_type}")

    if camera_type not in ['main', 'side']:
        return jsonify({"error": "Invalid camera type"}), 400

    settings_data = get_settings()
    camera_settings = settings_data.get('camera_params', {}).get(camera_type, {})

    if not camera_settings:
        app.logger.warning(f"No settings found for {camera_type} camera.")
        return jsonify({"error": "No settings found"}), 404

    app.logger.info(f"Sending {camera_type} camera settings to frontend: {camera_settings}")
    return jsonify(camera_settings), 200

@app.route('/api/set-centered-offset', methods=['POST'])
def set_centered_offset_route():
    global camera
    if camera:
        centered_offsets = set_centered_offset(camera)
        return jsonify(centered_offsets), 200
    else:
        return jsonify({"error": "Camera not connected"}), 400
    
@app.route('/api/camera-name', methods=['GET'])
def get_camera_name():
    try:
        if camera:
            return jsonify({'name': camera.GetDeviceInfo().GetModelName()}), 200
        else:
            return jsonify({'name': 'No camera connected'}), 200
    except Exception as e:
        app.logger.exception("Failed to get camera name")
        return jsonify({'error': str(e)}), 500

@app.route('/api/save-image', methods=['POST'])
def save_image():
    global handler
    try:
        data = request.get_json()
        save_directory = data.get('save_directory', '').strip()

        app.logger.info(f"Received save directory: {save_directory}")

        if not save_directory:
            raise ValueError("Save directory is empty")

        if not os.path.exists(save_directory):
            app.logger.info(f"Creating directory: {save_directory}")
            os.makedirs(save_directory)

        handler.folder_selected = save_directory
        handler.set_save_next_frame()
        app.logger.info("Triggered handler to save the next frame")

        # Wait for the image to be saved
        while not handler.saved_image_path:
            pass
        
        saved_image_path = os.path.join(save_directory, handler.get_latest_image_name())

        return jsonify({'message': 'Image saved', 'filename': os.path.basename(saved_image_path)}), 200
    except Exception as e:
        app.logger.exception("Failed to save image")
        return jsonify({'error': str(e)}), 500
    

def stop_camera_stream(camera_type):
    if camera_type not in cameras:
        raise ValueError(f"Invalid camera type: {camera_type}")

    camera = cameras.get(camera_type)

    with grab_locks[camera_type]:
        if not stream_running.get(camera_type, False):
            return "Stream already stopped."

        try:
            stream_running[camera_type] = False
            if camera and camera.IsGrabbing():
                camera.StopGrabbing()
                app.logger.info(f"{camera_type.capitalize()} camera stream stopped.")

            if stream_threads.get(camera_type) and stream_threads[camera_type].is_alive():
                stream_threads[camera_type].join(timeout=2)
                app.logger.info(f"{camera_type.capitalize()} stream thread stopped.")

            stream_threads[camera_type] = None
            return f"{camera_type.capitalize()} stream stopped."
        except Exception as e:
            raise RuntimeError(f"Failed to stop {camera_type} stream: {str(e)}")
        
def connect_camera_internal(camera_type):
    target_serial = CAMERA_IDS.get(camera_type)
    factory = pylon.TlFactory.GetInstance()
    devices = factory.EnumerateDevices()

    if not devices:
        return {"error": "No cameras connected"}

    selected_device = next((device for device in devices if device.GetSerialNumber() == target_serial), None)

    if not selected_device:
        return {"error": f"Camera {camera_type} with serial {target_serial} not found"}

    if cameras.get(camera_type) and cameras[camera_type].IsOpen():
        return {
            "connected": True,
            "name": selected_device.GetModelName(),
            "serial": selected_device.GetSerialNumber()
        }

    cameras[camera_type] = pylon.InstantCamera(factory.CreateDevice(selected_device))
    cameras[camera_type].Open()

    if not cameras[camera_type].IsOpen():
        return {"error": f"Camera {camera_type} failed to open"}

    camera_properties[camera_type] = get_camera_properties(cameras[camera_type])
    settings_data = get_settings()
    apply_camera_settings(camera_type, cameras, camera_properties, settings_data)

    return {
        "connected": True,
        "name": selected_device.GetModelName(),
        "serial": selected_device.GetSerialNumber()
    }

def start_camera_stream_internal(camera_type, scale_factor=0.25):
    app.logger.info(f"Starting {camera_type} camera stream internally with scale factor {scale_factor}")

    if camera_type not in cameras or cameras[camera_type] is None or not cameras[camera_type].IsOpen():
        app.logger.error(f"{camera_type.capitalize()} camera is not connected or open.")
        return {"error": f"{camera_type.capitalize()} camera not connected"}

    with grab_locks[camera_type]:
        if stream_running.get(camera_type, False):
            app.logger.info(f"{camera_type.capitalize()} stream is already running.")
            return {"message": "Stream already running"}

        if not cameras[camera_type].IsGrabbing():
            app.logger.info(f"{camera_type.capitalize()} camera starting grabbing.")
            cameras[camera_type].StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

        if not stream_threads.get(camera_type) or not stream_threads[camera_type].is_alive():
            app.logger.info(f"Starting new thread for {camera_type}")
            stream_running[camera_type] = True
        else:
            app.logger.info(f"{camera_type.capitalize()} stream thread already running.")

    return {"message": f"{camera_type.capitalize()} video stream started successfully."}
        
def initialize_cameras():
    app.logger.info("Initializing cameras...")
    for camera_type in CAMERA_IDS.keys():
        if cameras.get(camera_type) and cameras[camera_type].IsOpen():
            app.logger.info(f"{camera_type.capitalize()} camera is already connected. Skipping initialization.")
            continue
        
        try:
            result = connect_camera_internal(camera_type)
            if result.get('connected'):
                app.logger.info(f"Successfully connected {camera_type} camera.")
                start_camera_stream_internal(camera_type)
            else:
                app.logger.error(f"Failed to connect {camera_type} camera: {result.get('error')}")
        except Exception as e:
            app.logger.error(f"Error during {camera_type} camera initialization: {e}")

if __name__ == '__main__':      
    load_settings()
    initialize_cameras()
    app.run(debug=True, use_reloader=False)
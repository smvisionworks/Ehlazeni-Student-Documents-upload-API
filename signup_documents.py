from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, db, storage
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ----------------------------------
#  Upload Config (Local fallback)
# ----------------------------------
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads", "applications")
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024   # 20MB

# ----------------------------------
#  Firebase Initialization (robust)
# ----------------------------------
firebase_inited = False
firebase_bucket = None
try:
    # support either env name (matching your two services)
    service_key = os.environ.get("SERVICE_ACCOUNT_KEY") or os.environ.get("FIREBASE_SERVICE_ACCOUNT")

    if not service_key:
        print("ERROR: SERVICE_ACCOUNT_KEY or FIREBASE_SERVICE_ACCOUNT env var missing.")
    else:
        # service_key must be a JSON string
        svc = json.loads(service_key)
        # determine storage bucket name
        env_bucket = os.environ.get("FIREBASE_STORAGE_BUCKET")
        inferred_bucket = None
        if not env_bucket:
            project_id = svc.get("project_id")
            if project_id:
                inferred_bucket = f"{project_id}.appspot.com"

        storage_bucket_name = env_bucket or inferred_bucket
        if not storage_bucket_name:
            print("WARNING: FIREBASE_STORAGE_BUCKET env var not set and could not infer bucket name from service account. Firebase Storage will not be used.")
            # initialize app without storageBucket (still allows RTDB)
            cred = credentials.Certificate(svc)
            firebase_admin.initialize_app(cred, {
                "databaseURL": "https://ehlazeni-star-school-default-rtdb.firebaseio.com/"
            })
        else:
            cred = credentials.Certificate(svc)
            firebase_admin.initialize_app(cred, {
                "databaseURL": "https://ehlazeni-star-school-default-rtdb.firebaseio.com/",
                "storageBucket": storage_bucket_name
            })
            firebase_bucket = storage.bucket()  # google.cloud.storage.bucket.Bucket instance

        firebase_inited = True
        print("Firebase initialized successfully. Storage bucket:", storage_bucket_name if storage_bucket_name else "none")
except Exception as e:
    # Do NOT re-raise — keep the server running, but mark firebase_inited False
    print("Firebase initialization failed:", str(e))
    firebase_inited = False
    firebase_bucket = None

# ----------------------------------
#  Helpers
# ----------------------------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file_and_get_url(file_obj, host_url):
    """
    Upload file to Firebase Storage if available, otherwise save locally.
    Returns (public_url, stored_name, size_bytes)
    """
    # prepare unique name
    filename = secure_filename(file_obj.filename)
    unique = f"{int(datetime.now().timestamp()*1000)}_{uuid.uuid4().hex}_{filename}"
    stored_path = f"applications/{unique}"  # logical path in bucket

    # Attempt Firebase Storage upload if bucket exists
    if firebase_bucket:
        try:
            # read file bytes (safe up to MAX_CONTENT_LENGTH which is 20MB)
            file_obj.stream.seek(0)
            data = file_obj.read()
            blob = firebase_bucket.blob(stored_path)
            # upload bytes
            blob.upload_from_string(data, content_type=file_obj.mimetype or 'application/octet-stream')
            # make public for easy retrieval (requires the account to have permissions to change ACL)
            try:
                blob.make_public()
                public_url = blob.public_url
            except Exception:
                # If make_public fails (e.g. permissions), fall back to gs:// path
                public_url = f"gs://{firebase_bucket.name}/{stored_path}"

            size = len(data)
            return public_url, stored_path, size
        except Exception as e:
            # log and fall back to local save
            print("Firebase upload failed, falling back to local file store:", str(e))

    # Fallback: save locally (original behaviour)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    dest = os.path.join(app.config['UPLOAD_FOLDER'], unique)
    file_obj.stream.seek(0)
    file_obj.save(dest)
    size = os.path.getsize(dest)
    # Public URL using route (Render sometimes strips trailing slash)
    return f"{host_url.rstrip('/')}/uploads/applications/{unique}", unique, size


# ----------------------------------
#  Upload Documents
# ----------------------------------
@app.route('/upload-documents', methods=['POST'])
def upload_documents():
    if not firebase_inited:
        # We still allow non-firebase fallback uploads, but the original code returned 500 when firebase not inited.
        # To preserve behavior you had before, keep the same error response.
        return jsonify({'success': False, 'error': 'Server firebase not initialized. Check env var.'}), 500

    try:
        uid = request.form.get('uid')
        if not uid:
            return jsonify({'success': False, 'error': 'Missing uid'}), 400

        expected = ['previousResults', 'studentIdCopy', 'guardianIdCopy']
        missing = [k for k in expected if k not in request.files or request.files[k].filename == ""]

        if missing:
            return jsonify({'success': False, 'error': f"Missing files: {', '.join(missing)}"}), 400

        host = request.host_url

        documents = {}
        meta = {}

        for key in expected:
            file_obj = request.files[key]

            if not allowed_file(file_obj.filename):
                return jsonify({'success': False, 'error': f"Invalid file type for {key}"}), 400

            url, stored, size = save_file_and_get_url(file_obj, host)
            documents[key] = url
            meta[key] = {
                "originalName": file_obj.filename,
                "storedName": stored,
                "size": size
            }

        # Save to Firebase RTDB
        ref = db.reference(f'application/pending/{uid}')
        ref.update({
            "documents": documents,
            "documentsMeta": meta,
            "documentsUploadedAt": datetime.utcnow().isoformat()
        })

        return jsonify({'success': True, 'documents': documents, 'meta': meta})

    except Exception as e:
        print("upload-documents error:", str(e))
        return jsonify({'success': False, 'error': str(e)}), 500


# ----------------------------------
#  fetch Files
# ----------------------------------
@app.route('/get-documents', methods=['GET'])
def get_documents():
    if not firebase_inited:
        return jsonify({'success': False, 'error': 'Server firebase not initialized. Check env var.'}), 500

    try:
        uid = request.args.get('uid')
        if not uid:
            return jsonify({'success': False, 'error': 'Missing uid'}), 400

        ref = db.reference(f'application/pending/{uid}')
        data = ref.get()

        if not data:
            return jsonify({'success': False, 'error': 'No documents found'}), 404

        return jsonify({'success': True, 'data': data})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ----------------------------------
#  Serve Files (local fallback only)
# ----------------------------------
@app.route('/uploads/applications/<path:filename>')
def serve_uploaded_file(filename):
    # send_from_directory will 404 if not present — this route is only useful if local fallback was used.
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ----------------------------------
#  Render / Gunicorn Entry
# ----------------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, debug=True)

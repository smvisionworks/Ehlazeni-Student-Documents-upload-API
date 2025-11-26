from flask import Flask, request, jsonify, send_from_directory
import os
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, db
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# ----------------------------------
#  Upload Config (Local on Render)
# ----------------------------------
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads", "applications")
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024   # 20MB

# ----------------------------------
#  Firebase Initialization (robust)
# ----------------------------------
firebase_inited = False
try:
    # support either env name (matching your two services)
    service_key = os.environ.get("SERVICE_ACCOUNT_KEY") or os.environ.get("FIREBASE_SERVICE_ACCOUNT")

    if not service_key:
        print("ERROR: SERVICE_ACCOUNT_KEY or FIREBASE_SERVICE_ACCOUNT env var missing.")
    else:
        # service_key must be a JSON string
        svc = json.loads(service_key)
        cred = credentials.Certificate(svc)
        firebase_admin.initialize_app(cred, {
            "databaseURL": "https://ehlazeni-star-school-default-rtdb.firebaseio.com/"
        })
        firebase_inited = True
        print("Firebase initialized successfully.")
except Exception as e:
    # Do NOT re-raise — keep the server running, but mark firebase_inited False
    print("Firebase initialization failed:", str(e))
    firebase_inited = False

# ----------------------------------
#  Helpers
# ----------------------------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file_and_get_url(file_obj, host_url):
    # ensure upload dir exists now (create at runtime)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    filename = secure_filename(file_obj.filename)
    unique = f"{int(datetime.now().timestamp()*1000)}_{uuid.uuid4().hex}_{filename}"

    dest = os.path.join(app.config['UPLOAD_FOLDER'], unique)
    file_obj.save(dest)

    # Public URL using route (Render sometimes strips trailing slash)
    return f"{host_url.rstrip('/')}/uploads/applications/{unique}", unique, os.path.getsize(dest)


# ----------------------------------
#  Upload Documents
# ----------------------------------
@app.route('/upload-documents', methods=['POST'])
def upload_documents():
    if not firebase_inited:
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
#  Serve Files
# ----------------------------------
@app.route('/uploads/applications/<path:filename>')
def serve_uploaded_file(filename):
    # send_from_directory will 404 if not present
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ----------------------------------
#  Render / Gunicorn Entry
# ----------------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, debug=True)

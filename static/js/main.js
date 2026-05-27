/**
 * Lumina Skin - Core Frontend JS Controller
 * Handles camera stream, drag-and-drop, interactive loader, multi-stage forms,
 * and Firestore credit check + deduction BEFORE face analysis is initiated.
 *
 * Firebase v10 Modular SDK is loaded via a companion <script type="module">
 * that exposes `window.__luminaFirebase` for use here.
 *
 * Credit flow:
 *   1. User submits image → uploadAndAnalyze() is called
 *   2. Read user's current credits from Firestore
 *   3. If credits < 10 → show error, open pricing modal, ABORT
 *   4. If credits >= 10 → deduct 10 in Firestore atomically
 *   5. POST to /analyze with the post-deduction credits value
 *   6. On success → run loading screen → show details form
 */

// ── Firebase Initialisation (inline module) ──────────────────────────────────
// We load Firebase here using a dynamic import so main.js stays non-module
// (required for the legacy <script src="..."> tag on upload.html).
// The resolved auth + db instances are stored on window.__luminaFirebase.

(async function initFirebase() {
    try {
        const { initializeApp } = await import(
            "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js"
        );
        const { getAuth, onAuthStateChanged } = await import(
            "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js"
        );
        const { getFirestore, doc, getDoc, updateDoc, increment } = await import(
            "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js"
        );

        const firebaseConfig = {
            apiKey:            "AIzaSyAA0DmT_ld_FWiwWnnz78e6XbvWs-B9eFg",
            authDomain:        "lumina-6d262.firebaseapp.com",
            projectId:         "lumina-6d262",
            storageBucket:     "lumina-6d262.firebasestorage.app",
            messagingSenderId: "397169702043",
            appId:             "1:397169702043:web:27f6c78380bb534273823e",
            measurementId:     "G-E7X424RWLK"
        };

        const app  = initializeApp(firebaseConfig);
        const auth = getAuth(app);
        const db   = getFirestore(app);

        // Expose helpers globally so the DOMContentLoaded block can use them
        window.__luminaFirebase = { auth, db, doc, getDoc, updateDoc, increment, onAuthStateChanged };

    } catch (err) {
        console.error("[Lumina] Firebase init failed:", err);
        window.__luminaFirebase = null;
    }
})();


// ── Main Controller ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

    // -----------------------------------------------------------------
    // DOM Selectors
    // -----------------------------------------------------------------
    const uploadStage   = document.getElementById('upload-stage');
    const loadingStage  = document.getElementById('loading-stage');
    const detailsStage  = document.getElementById('details-stage');

    const dropZone      = document.getElementById('drop-zone');
    const fileInput     = document.getElementById('file-input');
    const btnBrowse     = document.getElementById('btn-browse');
    const btnCameraOpen = document.getElementById('btn-camera-open');
    const btnCameraClose= document.getElementById('btn-camera-close');
    const cameraContainer = document.getElementById('camera-container');
    const videoStream   = document.getElementById('video-stream');
    const btnCapture    = document.getElementById('btn-capture');

    const previewWrapper= document.getElementById('preview-wrapper');
    const imagePreview  = document.getElementById('image-preview');
    const scanOverlay   = document.getElementById('scan-overlay');
    const errorBox      = document.getElementById('error-box');

    const loadingTitle  = document.getElementById('loading-title');
    const loadingDesc   = document.getElementById('loading-desc');
    const progressCircle= document.getElementById('progress-circle');

    const detailsForm   = document.getElementById('details-form');

    // State
    let localStream         = null;
    let base64CapturedImage = null;
    const ANALYSIS_COST     = 10;

    // -----------------------------------------------------------------
    // Drag & Drop / File Browser Setup
    // -----------------------------------------------------------------
    btnBrowse.addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) handleFileSelect(e.target.files[0]);
    });

    ['dragenter', 'dragover'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        }, false);
    });

    dropZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) handleFileSelect(files[0]);
    });

    function handleFileSelect(file) {
        errorBox.style.display = 'none';

        const validTypes = ['image/jpeg', 'image/png', 'image/webp'];
        if (!validTypes.includes(file.type)) {
            showError("Invalid file type. Please upload a JPG, PNG, or WEBP photograph.");
            return;
        }
        if (file.size > 10 * 1024 * 1024) {
            showError("The selected photo exceeds the 10MB limit. Please upload a smaller image.");
            return;
        }

        const reader = new FileReader();
        reader.onload = (e) => {
            base64CapturedImage = null;
            imagePreview.src    = e.target.result;
            previewWrapper.style.display = 'block';
            uploadAndAnalyze(file, null);
        };
        reader.readAsDataURL(file);
    }

    // -----------------------------------------------------------------
    // Webcam Capture Setup
    // -----------------------------------------------------------------
    btnCameraOpen.addEventListener('click', async () => {
        errorBox.style.display = 'none';
        try {
            localStream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } },
                audio: false
            });
            videoStream.srcObject    = localStream;
            dropZone.style.display   = 'none';
            cameraContainer.style.display = 'block';
        } catch (err) {
            console.error("Camera access failed:", err);
            showError("Unable to access your device camera. Please upload an image from your gallery instead.");
        }
    });

    btnCameraClose.addEventListener('click', stopCameraStream);

    function stopCameraStream() {
        if (localStream) localStream.getTracks().forEach(track => track.stop());
        cameraContainer.style.display = 'none';
        dropZone.style.display        = 'block';
    }

    btnCapture.addEventListener('click', () => {
        const canvas  = document.createElement('canvas');
        canvas.width  = videoStream.videoWidth  || 640;
        canvas.height = videoStream.videoHeight || 480;
        const ctx     = canvas.getContext('2d');
        ctx.translate(canvas.width, 0);
        ctx.scale(-1, 1);
        ctx.drawImage(videoStream, 0, 0, canvas.width, canvas.height);

        const base64Data    = canvas.toDataURL('image/jpeg', 0.95);
        base64CapturedImage = base64Data;

        imagePreview.src             = base64Data;
        previewWrapper.style.display = 'block';

        stopCameraStream();
        uploadAndAnalyze(null, base64Data);
    });

    // -----------------------------------------------------------------
    // CREDIT GATE — Firestore Check & Deduction
    // -----------------------------------------------------------------
    /**
     * Returns the current Firebase UID if the user is signed in, or null.
     * Uses the auth instance from the async init block.
     */
    function getFirebaseUID() {
        if (!window.__luminaFirebase) return null;
        const { auth } = window.__luminaFirebase;
        return auth.currentUser ? auth.currentUser.uid : null;
    }

    /**
     * Checks Firestore for the user's credits.
     * If credits < ANALYSIS_COST → shows error, opens pricing modal, returns false.
     * If credits >= ANALYSIS_COST → atomically deducts 10, returns the new credit count.
     * Returns false on any failure.
     */
    async function checkAndDeductCredits() {
        const fb = window.__luminaFirebase;

        // Fallback: if Firebase isn't loaded, let the server handle the gate
        if (!fb) return { ok: true, updatedCredits: null };

        const uid = getFirebaseUID();
        if (!uid) {
            // User not logged in — redirect to login
            window.location.href = '/login';
            return { ok: false };
        }

        try {
            const { db, doc, getDoc, updateDoc, increment } = fb;
            const userRef  = doc(db, 'users', uid);
            const snapshot = await getDoc(userRef);

            if (!snapshot.exists()) {
                showError("Account not found. Please sign in again.");
                return { ok: false };
            }

            const currentCredits = snapshot.data().credits ?? 0;

            // Update the credits banner in real-time
            updateCreditDisplays(currentCredits);

            if (currentCredits < ANALYSIS_COST) {
                showError(
                    `Not enough credits. You need ${ANALYSIS_COST} credits per scan but only have ${currentCredits}. ` +
                    `Please purchase more credits.`
                );
                // Auto-open the pricing modal after a short delay
                setTimeout(() => {
                    if (typeof openPricingModal === 'function') openPricingModal();
                }, 800);
                return { ok: false };
            }

            // Deduct atomically — Firestore increment prevents race conditions
            await updateDoc(userRef, { credits: increment(-ANALYSIS_COST) });
            const updatedCredits = currentCredits - ANALYSIS_COST;

            // Update UI credit displays immediately
            updateCreditDisplays(updatedCredits);

            return { ok: true, updatedCredits };

        } catch (err) {
            console.error("[Lumina] Firestore credit check failed:", err);
            // Fail-open: let backend handle final gate; log the error
            return { ok: true, updatedCredits: null };
        }
    }

    /** Updates all credit display elements in the page. */
    function updateCreditDisplays(credits) {
        ['nav-credits-val', 'credits-display'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = credits;
        });
    }

    // -----------------------------------------------------------------
    // AJAX Core: uploadAndAnalyze (with Firestore credit gate)
    // -----------------------------------------------------------------
    async function uploadAndAnalyze(file, base64Data) {
        scanOverlay.style.display = 'block';

        // ── STEP 1: Credit gate ──────────────────────────────────────
        const creditResult = await checkAndDeductCredits();
        if (!creditResult.ok) {
            scanOverlay.style.display    = 'none';
            previewWrapper.style.display = 'none';
            imagePreview.src             = '#';
            return;
        }

        // ── STEP 2: Build request ────────────────────────────────────
        let requestOptions = {};

        if (base64Data) {
            const payload = { camera_image: base64Data };
            if (creditResult.updatedCredits !== null) {
                payload.updated_credits = creditResult.updatedCredits;
            }
            requestOptions = {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(payload)
            };
        } else {
            const formData = new FormData();
            formData.append('image', file);
            if (creditResult.updatedCredits !== null) {
                formData.append('updated_credits', creditResult.updatedCredits);
            }
            requestOptions = { method: 'POST', body: formData };
        }

        // ── STEP 3: Send to backend ──────────────────────────────────
        setTimeout(async () => {
            try {
                const response = await fetch('/analyze', requestOptions);
                const result   = await response.json();

                if (!result.success) {
                    showError(result.error || "Analysis failed. Please try again.");
                    scanOverlay.style.display    = 'none';
                    previewWrapper.style.display = 'none';

                    // If the backend also flags insufficient credits, open modal
                    if (result.credits_required && typeof openPricingModal === 'function') {
                        setTimeout(openPricingModal, 1000);
                    }
                    return;
                }

                startLoadingScreenSequence(result);

            } catch (err) {
                console.error("AJAX call error:", err);
                showError("Connection timeout. Please verify backend state.");
                scanOverlay.style.display    = 'none';
                previewWrapper.style.display = 'none';
            }
        }, 1200);
    }

    // -----------------------------------------------------------------
    // High-End Interactive Scientific Loading Screen Simulation
    // -----------------------------------------------------------------
    function startLoadingScreenSequence(analysisResults) {
        uploadStage.style.display  = 'none';
        loadingStage.style.display = 'block';

        const statusSteps = [
            { pct: 20,  title: "Initializing Calibration Matrix",          desc: "Setting up spatial boundary coordinates..." },
            { pct: 45,  title: "Isolating 468 Face Mesh Nodes",            desc: "Locking cheek, jawline, forehead, and eye regions..." },
            { pct: 70,  title: "Analyzing Proportions & Contour Shapes",   desc: "Calculating geometric ratio arrays for face shape..." },
            { pct: 88,  title: "Performing Texture & Color Audits",        desc: "Scanning T-zone oils, redness clusters, and dark margins..." },
            { pct: 100, title: "Compiling Diagnostic Scores",              desc: "Finalizing hydration, clarity, and tone parameters..." }
        ];

        let stepIdx = 0;

        function runNextStep() {
            if (stepIdx >= statusSteps.length) {
                setTimeout(() => { transitionToDetailsForm(analysisResults); }, 800);
                return;
            }

            const current = statusSteps[stepIdx];
            loadingTitle.textContent = current.title;
            loadingDesc.textContent  = current.desc;

            const radius        = 70;
            const circumference = 2 * Math.PI * radius;
            const offset        = circumference - (current.pct / 100) * circumference;
            progressCircle.style.strokeDashoffset = offset;

            stepIdx++;
            setTimeout(runNextStep, 900);
        }

        runNextStep();
    }

    function transitionToDetailsForm(result) {
        loadingStage.style.display  = 'none';
        detailsStage.style.display  = 'block';

        const skinTypeVal   = result.detected_skin_type;
        const skinTypeInput = document.getElementById('skin-type-input');

        document.querySelectorAll('.skin-type-options .selector-option').forEach(btn => {
            btn.classList.remove('selected');
            if (btn.getAttribute('data-value') === skinTypeVal) {
                btn.classList.add('selected');
                skinTypeInput.value = skinTypeVal;
            }
        });

        if (result.detected_issues && result.detected_issues.length > 0) {
            result.detected_issues.forEach(issue => {
                const checkbox = document.querySelector(`.concerns-checkbox-grid input[value="${issue}"]`);
                if (checkbox) checkbox.checked = true;
            });
        }
    }

    // -----------------------------------------------------------------
    // Interactive Selector Buttons (Gender & Skin Type)
    // -----------------------------------------------------------------
    setupInteractiveSelector('.gender-options .selector-option',    '#gender-input');
    setupInteractiveSelector('.skin-type-options .selector-option', '#skin-type-input');

    function setupInteractiveSelector(selector, hiddenInputId) {
        const options     = document.querySelectorAll(selector);
        const hiddenInput = document.querySelector(hiddenInputId);

        options.forEach(opt => {
            opt.addEventListener('click', () => {
                options.forEach(o => o.classList.remove('selected'));
                opt.classList.add('selected');
                hiddenInput.value = opt.getAttribute('data-value');
            });
        });
    }

    // -----------------------------------------------------------------
    // User Details Form Submission
    // -----------------------------------------------------------------
    detailsForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const formPayload = new FormData(detailsForm);

        try {
            const response = await fetch('/submit-details', {
                method: 'POST',
                body:   formPayload
            });
            const resData = await response.json();

            if (resData.success) {
                window.location.href = `/result/${resData.user_id}`;
            } else {
                alert("Could not register profile. Please try again.");
            }
        } catch (err) {
            console.error("Error submitting final details form:", err);
            alert("Network connection error during database saving.");
        }
    });

    // Helper
    function showError(msg) {
        errorBox.textContent    = msg;
        errorBox.style.display  = 'block';
        window.scrollTo({ top: errorBox.offsetTop - 120, behavior: 'smooth' });
    }
});

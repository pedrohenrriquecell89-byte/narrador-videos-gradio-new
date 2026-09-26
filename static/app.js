const form = document.getElementById("form");
const submitBtn = document.getElementById("submitBtn");
const statusBox = document.getElementById("status");
const statusMsg = document.getElementById("statusMsg");
const progressFill = document.getElementById("progressFill");
const errorBox = document.getElementById("error");
const downloadBtn = document.getElementById("downloadBtn");
const imagesInput = document.getElementById("images");
const preview = document.getElementById("preview");

imagesInput.addEventListener("change", () => {
  preview.innerHTML = "";
  [...imagesInput.files].forEach(file => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    preview.appendChild(img);
  });
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.classList.add("hidden");
  downloadBtn.classList.add("hidden");

  const script = document.getElementById("script").value;
  const files = imagesInput.files;

  if (!script.trim()) {
    showError("Please write a script.");
    return;
  }
  if (!files.length) {
    showError("Please select at least one photo.");
    return;
  }

  const fd = new FormData();
  fd.append("script", script);
  [...files].forEach(f => fd.append("images", f));

  submitBtn.disabled = true;
  submitBtn.textContent = "Uploading...";

  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || "Upload failed.");
      resetButton();
      return;
    }
    statusBox.classList.remove("hidden");
    pollStatus(data.job_id);
  } catch (err) {
    showError("Network error. Check your connection and try again.");
    resetButton();
  }
});

function pollStatus(jobId) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/status/${jobId}`);
      if (!res.ok) throw new Error("status check failed");
      const s = await res.json();

      progressFill.style.width = `${s.progress}%`;
      statusMsg.textContent = s.message || s.stage;

      if (s.error) {
        clearInterval(interval);
        showError(s.error);
        resetButton();
      } else if (s.done && s.download_ready) {
        clearInterval(interval);
        statusMsg.textContent = "Done!";
        downloadBtn.href = `/download/${jobId}`;
        downloadBtn.classList.remove("hidden");
        resetButton();
      }
    } catch (err) {
      // Render free tier can sleep/wake; keep polling instead of failing hard.
    }
  }, 3000);
}

function showError(msg) {
  errorBox.textContent = "⚠️ " + msg;
  errorBox.classList.remove("hidden");
}

function resetButton() {
  submitBtn.disabled = false;
  submitBtn.textContent = "Generate Video";
}

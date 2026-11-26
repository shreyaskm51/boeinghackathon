import React from "react";

export default function Toast({ message, onClose }) {
  if (!message) return null;
  return (
    <div className="glass toast" role="alert">
      <span>{message}</span>
      <button type="button" className="toast-close" onClick={onClose} aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

import React, { useEffect, useState } from "react";

export default function SmsPanel({ alert }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return undefined;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);

  if (!alert) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(alert);
      setCopied(true);
    } catch {
      // clipboard blocked by the browser: the text is still visible to copy by hand
    }
  };

  return (
    <section className="glass panel sms-panel" aria-label="Port master SMS">
      <div className="sms-head">
        <span className="panel-title">Port master SMS · {alert.length}/160</span>
        <button type="button" className="copy-btn" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="sms-text">{alert}</div>
    </section>
  );
}

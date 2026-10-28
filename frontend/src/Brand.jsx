import React from "react";
import mascot from "./assets/moov-mascot.png";

// Decorative beside the wordmark or heading: the adjacent text names the site.
export function Mascot({ className = "" }) {
  return <img className={`moov-mascot ${className}`} src={mascot} alt="" aria-hidden="true" width="1024" height="1024" />;
}

export function BrandIdentity() {
  return <span className="brand-identity"><Mascot /><span>My Next Moov</span></span>;
}

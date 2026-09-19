import React from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

function App() {
  return <main><p className="eyebrow">HYPE AI STUDIO / 001A</p><h1>Local production lab</h1><p>Async video generation and FFmpeg composition are ready for the next workflow slice.</p><div className="status"><span /> Mock provider online</div></main>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);

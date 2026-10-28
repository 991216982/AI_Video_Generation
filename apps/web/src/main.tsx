import { createRoot } from "react-dom/client";
import React from "react";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <main style={{ fontFamily: "sans-serif", padding: "2rem" }}>
      <h1>图书短视频工作台</h1>
      <p>阶段 0：前端开发环境已启动。业务页面将在阶段 1 接入。</p>
    </main>
  </React.StrictMode>,
);

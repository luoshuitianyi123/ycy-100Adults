// ==UserScript==
// @name         ycy-100Adults 页面桥接
// @namespace    ycy.bridge
// @version      1.0.0
// @description  在目标页面读取 BPM / 档位等数值，转发到本地后端；仅读取数值，不处理其他内容。
// 按目标页面域名配置 @match，例如：
// @match        https://<target-site>/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*
 * 备选取数通道：在已打开的页面里读取渲染到 DOM 的 BPM / 档位，转发到本地后端。
 * 本地后端地址默认 ws://127.0.0.1:8799，可在下方 LOCAL_BASE 修改。
 */

(function () {
  "use strict";

  const LOCAL_BASE = "ws://127.0.0.1:8799";      // 本地后端 WS 地址
  const HTTP_BASE = "http://127.0.0.1:8799";     // WS 不可用时的 HTTP 备选
  const BPM_MIN = 20, BPM_MAX = 300;

  let ws = null;
  let lastBpm = null;
  let lastLevel = null;
  let lastSpeed = null;
  let lastGrip = null;
  let scanTimer = null;
  let tick = 0;
  // 心跳周期（秒）：即使数值没变，也按此周期重发当前值保活，避免后端把“稳定保持”误判为掉线停止
  const HEARTBEAT_EVERY = 2;

  // ---------- 本地连接 ----------
  function connectWS() {
    try {
      ws = new WebSocket(LOCAL_BASE + "/ws/bridge");
      ws.onopen = () => log("桥接 WS 已连接 " + LOCAL_BASE);
      ws.onclose = () => { ws = null; setTimeout(connectWS, 2000); };
      ws.onerror = () => { try { ws && ws.close(); } catch (e) {} };
    } catch (e) {
      setTimeout(connectWS, 2000);
    }
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    } else if (msg.type === "bpm") {
      // HTTP 备选
      fetch(HTTP_BASE + "/api/bridge/bpm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bpm: msg.bpm }),
      }).catch(() => {});
    }
  }

  const CMDS = ["fast", "slow", "stop", "go"];

  // ---------- DOM 取数 ----------
  function readBpm() {
    // 1) data-remote-bpm 属性（页面渲染的 BPM 徽标）
    const elR = document.querySelector("[data-remote-bpm]");
    let v = parseInt(elR ? elR.getAttribute("data-remote-bpm") : "", 10);
    if (isValidBpm(v)) return v;
    // 2) 文本正则：“100 BPM”
    const bodyText = document.body ? document.body.innerText : "";
    const m = bodyText.match(/(\d{2,3})\s*BPM/i);
    if (m) {
      v = parseInt(m[1], 10);
      if (isValidBpm(v)) return v;
    }
    return null;
  }

  function isValidBpm(v) {
    return Number.isInteger(v) && (v === 0 || (v >= BPM_MIN && v <= BPM_MAX));
  }

  function readLevel() {
    const btn = document.querySelector('[data-remote-level-btn][aria-pressed="true"]');
    if (btn) {
      const v = parseInt(btn.getAttribute("data-remote-level-btn"), 10);
      if (Number.isInteger(v) && v >= 1 && v <= 9) return v;
    }
    return null;
  }

  const GRIP_NAMES = ["feather", "soft", "normal", "hard", "harder"];

  function _onCount(selector) {
    const el = document.querySelector(selector);
    if (!el) return null;
    return el.querySelectorAll("i.on").length;
  }

  // 速度段 rp-seg：6 个点，点亮个数 0–5（0=停止）
  function readSpeed() {
    const n = _onCount(".rp-seg");
    return n === null ? null : Math.min(5, n);
  }

  function readGrip() {
    // 1) 档位名属性
    const el = document.querySelector("[data-remote-grip-now]");
    const v = (el ? el.getAttribute("data-remote-grip-now") : "" || "").toLowerCase();
    if (GRIP_NAMES.includes(v)) return v;
    // 2) 点条 rp-dot5：5 个点，点亮个数 0–4（0=停止）
    const dots = _onCount(".rp-dot5");
    return dots === null ? null : Math.min(4, dots);
  }

  function readCmd() {
    for (const name of ["data-remote-command"]) {
      const el = document.querySelector(`[${name}]`);
      const v = (el ? el.getAttribute(name) : "" || "").toLowerCase();
      if (CMDS.includes(v)) return v;
    }
    return null;
  }

  let lastCmd = null;
  let bpmMiss = 0;
  const MISS_STOP = 2; // 连续 N 次读不到 BPM，视为无数据并停止

  function scan() {
    tick++;
    const hb = tick % HEARTBEAT_EVERY === 0; // 到心跳周期：重发当前值保活

    const bpm = readBpm();
    if (bpm !== null) {
      bpmMiss = 0;
      if (bpm !== lastBpm) {
        lastBpm = bpm;
        send({ type: "bpm", bpm });
        log("BPM → " + bpm);
      } else if (hb) {
        send({ type: "bpm", bpm }); // 心跳：数值不变也重发，刷新后端在线时间戳
      }
    } else {
      // 页面无数据：上报停止，而不是保持上次强度
      bpmMiss += 1;
      if (bpmMiss >= MISS_STOP && lastBpm !== null && lastBpm !== 0) {
        bpmMiss = 0;
        lastBpm = 0;
        send({ type: "bpm", bpm: 0 });
        log("无数据 → 停止");
      }
    }
    const level = readLevel();
    if (level !== null) {
      if (level !== lastLevel) {
        lastLevel = level;
        send({ type: "level", level });
        log("档位 → " + level);
      } else if (hb) {
        send({ type: "level", level });
      }
    }
    const speed = readSpeed();
    if (speed !== null) {
      if (speed !== lastSpeed) {
        lastSpeed = speed;
        send({ type: "speed", speed });
        log("速度档 → " + speed);
      } else if (hb) {
        send({ type: "speed", speed });
      }
    }
    const grip = readGrip();
    if (grip !== null) {
      if (grip !== lastGrip) {
        lastGrip = grip;
        send({ type: "grip", grip });
        log("信号 → " + grip);
      } else if (hb) {
        send({ type: "grip", grip });
      }
    }
    const cmd = readCmd();
    if (cmd !== null && cmd !== lastCmd) {
      lastCmd = cmd;
      send({ type: "cmd", cmd });
      log("指令 → " + cmd);
    }
  }

  function log(msg) {
    console.log("[fjb-bridge] " + msg);
  }

  // ---------- 启动 ----------
  connectWS();

  const observer = new MutationObserver(() => {
    if (scanTimer) return;
    scanTimer = setTimeout(() => { scanTimer = null; scan(); }, 300);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });

  scan();
  setInterval(scan, 1000);
})();

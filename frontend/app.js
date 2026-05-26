// app.js

const MAX_HISTORY = 6;
let history = [];
let sessionTotals = { gemmaIn: 0, gemmaOut: 0, flashIn: 0, flashOut: 0, flashCost: 0 };

// ── Session / Conversation ID ────────────────────────────────
function generateId() {
    return Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10);
}
function getSessionId() {
    let sid = localStorage.getItem("stp_session_id");
    if (!sid) { sid = generateId(); localStorage.setItem("stp_session_id", sid); }
    return sid;
}
let SESSION_ID = getSessionId();
let viewingSessionId = SESSION_ID; // which session is shown in main area
let _cachedSessions = [];          // last fetched sessions

// ── DOM Refs ─────────────────────────────────────────────────
const apiUrlInput = document.getElementById("apiUrlInput");
const pipelineRadios = document.getElementsByName("pipelineMode");
const exampleQueriesDiv = document.getElementById("exampleQueries");
const healthBadge = document.getElementById("healthBadge");
const messagesInner = document.getElementById("messagesInner");
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");
const conversationsList = document.getElementById("conversationsList");
const viewingBanner = document.getElementById("viewingBanner");
const viewingOverlay = document.getElementById("viewingOverlay");
const inputRow = document.getElementById("inputRow");
const returnToLiveBtn = document.getElementById("returnToLiveBtn");
const returnToLiveBtn2 = document.getElementById("returnToLiveBtn2");
const chatTitle = document.getElementById("chatTitle");
const chatSub = document.getElementById("chatSub");
const gemmaIn = document.getElementById("gemmaIn");
const gemmaOut = document.getElementById("gemmaOut");
const gemmaTok = document.getElementById("gemmaTok");
const flashIn = document.getElementById("flashIn");
const flashOut = document.getElementById("flashOut");
const flashTok = document.getElementById("flashTok");
const flashCost = document.getElementById("flashCost");
const totTok = document.getElementById("totTok");
const totCost = document.getElementById("totCost");
const lastRequestPanel = document.getElementById("lastRequestPanel");
const lastGemmaIn = document.getElementById("lastGemmaIn");
const lastGemmaOut = document.getElementById("lastGemmaOut");
const lastFlashIn = document.getElementById("lastFlashIn");
const lastFlashOut = document.getElementById("lastFlashOut");
const lastCost = document.getElementById("lastCost");
const intentPanel = document.getElementById("intentPanel");
const sqlPanel = document.getElementById("sqlPanel");

// ── Example Queries ──────────────────────────────────────────
const examples = [
    "What is the current flow rate?",
    "Show energy consumed by pump 2 today",
    "How many times did pump 1 start last week?",
    "Compare SEC of all pumps for May 2026",
    "What is the wet well level now?",
    "Show runtime of all pumps yesterday",
    "Power factor of pump 3 last 7 days",
    "How many pumps are running right now?"
];
examples.forEach(ex => {
    const btn = document.createElement("button");
    btn.className = "action-btn w-full";
    btn.style.textAlign = "left";
    btn.textContent = ex;
    btn.onclick = () => { userInput.value = ex; sendMessage(); };
    exampleQueriesDiv.appendChild(btn);
});

// ── New Chat ─────────────────────────────────────────────────
newChatBtn.onclick = () => {
    SESSION_ID = generateId();
    localStorage.setItem("stp_session_id", SESSION_ID);
    viewingSessionId = SESSION_ID;
    history = [];
    messagesInner.innerHTML = "";
    sessionTotals = { gemmaIn: 0, gemmaOut: 0, flashIn: 0, flashOut: 0, flashCost: 0 };
    updateTokenUI();
    setViewingMode(false);
    chatTitle.textContent = "New Chat";
    chatSub.textContent = "Sewage Treatment Plant · Intent-Aware SQL Generator";
    intentPanel.innerHTML = `<div class="empty-state">Ask a question to see intent analysis here</div>`;
    sqlPanel.innerHTML = `<div class="empty-state">SQL will appear here after a successful query</div>`;
    renderConversationsList(_cachedSessions);
    userInput.focus();
};

// ── Return to live chat ──────────────────────────────────────
function returnToCurrentChat() {
    viewingSessionId = SESSION_ID;
    setViewingMode(false);
    messagesInner.innerHTML = "";
    history = [];
    // Restore current session turns
    const mine = _cachedSessions.find(s => s.session_id === SESSION_ID);
    if (mine) {
        renderTurnsIntoChat(mine.turns);
        chatTitle.textContent = mine.turns[0]?.user || "New Chat";
    } else {
        chatTitle.textContent = "New Chat";
    }
    renderConversationsList(_cachedSessions);
}

if (returnToLiveBtn) returnToLiveBtn.onclick = returnToCurrentChat;
if (returnToLiveBtn2) returnToLiveBtn2.onclick = returnToCurrentChat;

function setViewingMode(isPast) {
    if (isPast) {
        viewingBanner.style.display = "flex";
        inputRow.style.display = "none";
        viewingOverlay.style.display = "block";
    } else {
        viewingBanner.style.display = "none";
        inputRow.style.display = "flex";
        viewingOverlay.style.display = "none";
    }
}

// ── Health Check ─────────────────────────────────────────────
async function checkHealth() {
    try {
        const url = apiUrlInput.value.replace(/\/$/, "");
        const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(2000) });
        healthBadge.className = res.ok ? "pill-on" : "pill-warn";
        healthBadge.textContent = res.ok ? "● API Online" : "● API Error";
    } catch {
        healthBadge.className = "pill-off";
        healthBadge.textContent = "● API Offline";
    }
}
setInterval(checkHealth, 10000);
checkHealth();
apiUrlInput.onchange = checkHealth;

// ── Send Message ─────────────────────────────────────────────
userInput.addEventListener("keydown", e => { if (e.key === "Enter") sendMessage(); });
sendBtn.onclick = sendMessage;

function getPipelineMode() {
    for (let r of pipelineRadios) if (r.checked) return r.value;
    return "full";
}

async function sendMessage() {
    if (viewingSessionId !== SESSION_ID) return; // block in view mode
    const text = userInput.value.trim();
    if (!text) return;

    appendUserBubble(text);
    userInput.value = "";

    // Update title to first question
    if (history.length === 0) chatTitle.textContent = text;

    const mode = getPipelineMode();
    const url = apiUrlInput.value.replace(/\/$/, "");
    const endpoint = mode === "intent_only" ? "/parse-intent" : "/query";
    const loaderId = appendThinkingBubble();

    try {
        const res = await fetch(`${url}${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ user_message: text, conversation_history: history.slice(-MAX_HISTORY) })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "API Error");

        // Token tracking
        if (mode === "intent_only") {
            const raw = data.raw_json?._usage || {};
            updateTokens(raw.promptTokenCount || 0, raw.candidatesTokenCount || 0, 0, 0, 0);
        } else {
            const tu = data.token_usage || {};
            updateTokens(
                (tu.intent_prompt_tokens || 0) + (tu.answer_prompt_tokens || 0),
                (tu.intent_output_tokens || 0) + (tu.answer_output_tokens || 0),
                tu.sql_prompt_tokens || 0, tu.sql_output_tokens || 0,
                tu.gemini_sql_total_cost_usd || 0
            );
        }

        let assistantText = "";
        let intentStr = "";

        if (mode === "intent_only") {
            updateBotBubble(loaderId, "✅ Intent parsed successfully. See the panel →", data.intent);
            renderIntentPanel(data);
            sqlPanel.innerHTML = `<div class="empty-state">SQL generation is disabled in Intent Only mode.</div>`;
            assistantText = "✅ Intent parsed successfully.";
            intentStr = data.intent || "";
        } else {
            if (data.clarification_needed) {
                updateBotBubble(loaderId, data.clarification_message, null, true, data.clarification_options || [], text);
                renderIntentPanel(data.intent_json);
                assistantText = data.clarification_message || "";
                intentStr = data.intent_json?.intent || "";
            } else if (!data.sql_query && data.clarification_message) {
                updateBotBubble(loaderId, data.clarification_message, data.intent_json?.intent);
                renderIntentPanel(data.intent_json);
                renderSqlPanel(data);
                assistantText = data.clarification_message || "";
                intentStr = data.intent_json?.intent || "";
            } else {
                updateBotBubble(loaderId, data.final_answer || "✅ SQL query generated.", data.intent_json?.intent, false, [], "", data.follow_up_questions || []);
                renderIntentPanel(data.intent_json);
                renderSqlPanel(data);
                assistantText = data.final_answer || "✅ SQL query generated.";
                intentStr = data.intent_json?.intent || "";
            }
        }

        history.push({ role: "user", content: text });
        history.push({ role: "assistant", content: assistantText });
        persistTurn(text, assistantText, intentStr);

    } catch (e) {
        updateBotBubble(loaderId, `❌ Error: ${e.message}`, "error");
    }
}

// ── Chat Bubble Helpers ──────────────────────────────────────
function appendUserBubble(text) {
    const div = document.createElement("div");
    div.className = "user-bubble";
    const timeDiv = document.createElement("div");
    timeDiv.style.cssText = "font-size:0.75rem;opacity:0.7;margin-bottom:4px;";
    timeDiv.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const textDiv = document.createElement("div");
    textDiv.textContent = text;
    div.appendChild(timeDiv);
    div.appendChild(textDiv);
    messagesInner.appendChild(div);
    scrollToBottom();
}

const THINKING_PHRASES = ["Pondering your query…", "Crunching the numbers…", "Consulting the database…", "Brewing an answer…", "Thinking hard…", "Parsing your intent…", "Almost there…", "Running the SQL…", "Connecting the dots…", "Analysing data…"];

function appendThinkingBubble() {
    const id = "msg-" + Date.now();
    const div = document.createElement("div");
    div.id = id;
    div.className = "thinking-bubble";
    div.innerHTML = `<div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-label" id="${id}-label">${THINKING_PHRASES[0]}</span>`;
    messagesInner.appendChild(div);
    scrollToBottom();
    let idx = 1;
    const label = div.querySelector(`#${id}-label`);
    const interval = setInterval(() => {
        if (!document.getElementById(id)) { clearInterval(interval); return; }
        label.style.opacity = "0";
        setTimeout(() => { label.textContent = THINKING_PHRASES[idx % THINKING_PHRASES.length]; label.style.opacity = "1"; idx++; }, 400);
    }, 1800);
    div._thinkingInterval = interval;
    return id;
}

function updateBotBubble(id, content, intent = null, isClarify = false, clarifyOptions = [], originalQuery = "", followUps = []) {
    const div = document.getElementById(id);
    if (!div) return;
    if (div._thinkingInterval) clearInterval(div._thinkingInterval);
    div.className = isClarify ? "clarify-bubble" : "bot-bubble";
    if (isClarify) {
        const iconMap = { today: '📅', yesterday: '⏮️', 'last 7 days': '📆', 'last 30 days': '🗓️', 'this month': '📋', 'last hour': '⏱️', 'last 24 hours': '🕐' };
        let html = `⚠️ <b>Clarification needed:</b><br>${content}`;
        if (clarifyOptions.length > 0) {
            html += `<div class="clarify-options"><span class="clarify-options-label">Quick select</span><div class="clarify-chips-row">`;
            clarifyOptions.forEach(opt => {
                html += `<button class="clarify-option-btn" data-opt="${opt}" onclick="handleClarifyOption('${opt.replace(/'/g, "\\'")}','${originalQuery.replace(/'/g, "\\'")}'">${iconMap[opt] || '🕒'} ${opt}</button>`;
            });
            html += `</div></div>`;
        }
        div.innerHTML = html;
    } else {
        div.innerHTML = formatBotContent(content, intent);
        if (followUps.length > 0) {
            const row = document.createElement("div");
            row.className = "followup-chips-row";
            followUps.forEach(q => {
                const btn = document.createElement("button");
                btn.className = "followup-chip";
                btn.textContent = q;
                btn.onclick = () => { userInput.value = q; sendMessage(); };
                row.appendChild(btn);
            });
            div.appendChild(row);
        }
    }
    scrollToBottom();
}

function handleClarifyOption(option, originalQuery) {
    userInput.value = `${originalQuery} ${option}`;
    sendMessage();
}

function formatBotContent(text, intent) {
    let res = "";
    if (intent && intent !== "error") res += `<div class="intent-badge">🎯 ${intent}</div>`;
    res += `<div>${renderMarkdown(text)}</div>`;
    return res;
}

function scrollToBottom() {
    const wrap = document.getElementById("messagesWrap");
    wrap.scrollTop = wrap.scrollHeight;
}

// ── Render Panels ────────────────────────────────────────────
function renderIntentPanel(intent) {
    if (!intent) return;
    let pumpsStr = "All";
    if (intent.pumps?.length > 0) pumpsStr = intent.pumps.join(", ");
    let html = `<div class="metrics-row">
        <div class="metric-card"><div class="metric-val">${(intent.intent || "—").replace(/_/g, " ").slice(0, 10)}</div><div class="metric-lbl">Intent</div></div>
        <div class="metric-card"><div class="metric-val">${pumpsStr}</div><div class="metric-lbl">Pumps</div></div>
        <div class="metric-card"><div class="metric-val">${(intent.aggregation || "—").toUpperCase()}</div><div class="metric-lbl">Agg</div></div>
    </div><div class="intent-flags">`;
    if (intent.requires_lag) html += `<span class="pill-warn">LAG Req</span>`;
    if (intent.clarification_needed) html += `<span class="pill-off">Needs Clarify</span>`;
    if (intent.limit) html += `<span class="pill-on">LIMIT ${intent.limit}</span>`;
    if (intent.group_by) html += `<span class="pill-on">GROUP BY</span>`;
    html += `</div><div class="intent-details">`;
    const tr = intent.time_range || {};
    if (tr.type !== "none") html += `<div>🕐 <b>Time Range:</b> ${tr.relative || (tr.start + " → " + tr.end)}</div>`;
    if (intent.metrics?.length > 0) html += `<div>📊 <b>Metrics:</b> ${intent.metrics.join(", ")}</div>`;
    html += `</div><div class="json-expander" onclick="this.classList.toggle('open')">
        <div class="json-expander-header">📋 Full Intent JSON <span>▼</span></div>
        <pre class="json-block">${JSON.stringify(intent, null, 2)}</pre>
    </div>`;
    intentPanel.innerHTML = html;
}

function renderSqlPanel(data) {
    if (!data.sql_query) { sqlPanel.innerHTML = `<div class="empty-state">No SQL generated for this query.</div>`; return; }
    let html = "";
    if (data.db_error) html += `<div class="error-banner">⚠️ DB Error: ${data.db_error}</div>`;
    html += `<div class="sql-block">${data.sql_query}</div>`;
    const rows = data.db_results ? data.db_results.length : 0;
    html += `<div class="sql-stats">
        <div class="sql-stat-btn sql-copy-btn" onclick="navigator.clipboard.writeText(\`${data.sql_query.replace(/`/g, '\\`')}\`)">📋 Copy</div>
        <div class="sql-stat-btn" style="color:${rows > 0 ? 'var(--accent-green)' : 'inherit'}">🗃️ ${rows} rows</div>
    </div>`;
    sqlPanel.innerHTML = html;
}

function updateTokens(g_in, g_out, f_in, f_out, f_cost) {
    sessionTotals.gemmaIn += g_in; sessionTotals.gemmaOut += g_out;
    sessionTotals.flashIn += f_in; sessionTotals.flashOut += f_out;
    sessionTotals.flashCost += f_cost;
    const allTok = sessionTotals.gemmaIn + sessionTotals.gemmaOut + sessionTotals.flashIn + sessionTotals.flashOut;
    gemmaIn.textContent = sessionTotals.gemmaIn.toLocaleString();
    gemmaOut.textContent = sessionTotals.gemmaOut.toLocaleString();
    gemmaTok.textContent = (sessionTotals.gemmaIn + sessionTotals.gemmaOut).toLocaleString();
    flashIn.textContent = sessionTotals.flashIn.toLocaleString();
    flashOut.textContent = sessionTotals.flashOut.toLocaleString();
    flashTok.textContent = (sessionTotals.flashIn + sessionTotals.flashOut).toLocaleString();
    flashCost.textContent = "$" + sessionTotals.flashCost.toFixed(6);
    totTok.textContent = allTok.toLocaleString();
    totCost.textContent = "$" + sessionTotals.flashCost.toFixed(6);
    if (g_in > 0 || g_out > 0 || f_in > 0 || f_out > 0) {
        lastRequestPanel.style.display = "block";
        lastGemmaIn.textContent = g_in.toLocaleString();
        lastGemmaOut.textContent = g_out.toLocaleString();
        lastFlashIn.textContent = f_in.toLocaleString();
        lastFlashOut.textContent = f_out.toLocaleString();
        lastCost.textContent = "$" + f_cost.toFixed(6);
    }
}

function updateTokenUI() {
    sessionTotals = { gemmaIn: 0, gemmaOut: 0, flashIn: 0, flashOut: 0, flashCost: 0 };
    [gemmaIn, gemmaOut, gemmaTok, flashIn, flashOut, flashTok, totTok].forEach(el => el.textContent = "0");
    flashCost.textContent = totCost.textContent = "$0.000000";
    lastRequestPanel.style.display = "none";
}

// ── Conversations Sidebar ────────────────────────────────────
function renderConversationsList(sessions) {
    _cachedSessions = sessions;
    if (!conversationsList) return;
    if (!sessions.length) {
        conversationsList.innerHTML = `<div class="conv-empty">No chats yet.<br>Start chatting!</div>`;
        return;
    }
    conversationsList.innerHTML = "";
    sessions.forEach((s, idx) => {
        const title = s.turns[0]?.user || "Untitled";
        const isActive = s.session_id === viewingSessionId;
        const isLive = s.session_id === SESSION_ID;
        const timeStr = (s.last_seen || "").slice(12, 17);

        const item = document.createElement("div");
        item.className = "conv-item" + (isActive ? " active" : "");
        item.style.animationDelay = `${idx * 0.04}s`;
        item.innerHTML = `
            <div class="conv-item-icon">${isLive ? "💬" : "🕐"}</div>
            <div class="conv-item-body">
                <div class="conv-item-title">${escapeHtml(title)}</div>
                <div class="conv-item-meta">
                    <span class="conv-item-turns">${s.turn_count} msg${s.turn_count !== 1 ? "s" : ""}</span>
                    ${timeStr ? `<span>${timeStr}</span>` : ""}
                    ${isLive ? `<span class="pill-on" style="font-size:0.65rem;padding:1px 6px;">live</span>` : ""}
                </div>
            </div>
            ${!isLive ? `<button class="continue-btn" title="Continue this chat">▶</button>` : ""}`;
        // Clicking the item body loads (view) it; Continue button switches live session
        item.querySelector(".conv-item-body").addEventListener("click", () => loadConversation(s));
        item.querySelector(".conv-item-icon").addEventListener("click", () => loadConversation(s));
        const continueBtn = item.querySelector(".continue-btn");
        if (continueBtn) continueBtn.addEventListener("click", (e) => { e.stopPropagation(); continueConversation(s); });
        conversationsList.appendChild(item);
    });
}

function loadConversation(session) {
    viewingSessionId = session.session_id;
    const isPast = session.session_id !== SESSION_ID;
    messagesInner.innerHTML = "";
    history = [];
    renderTurnsIntoChat(session.turns);
    chatTitle.textContent = session.turns[0]?.user || "Conversation";
    chatSub.textContent = `${session.turn_count} messages · ${session.source_ip || ""}`;
    setViewingMode(isPast);
    renderConversationsList(_cachedSessions);
    scrollToBottom();
}

/** Continue a past session: make it the active live session so the user can keep chatting from where they left off. */
function continueConversation(session) {
    SESSION_ID = session.session_id;
    localStorage.setItem("stp_session_id", SESSION_ID);
    viewingSessionId = SESSION_ID;
    messagesInner.innerHTML = "";
    history = [];
    renderTurnsIntoChat(session.turns);
    chatTitle.textContent = session.turns[0]?.user || "Conversation";
    chatSub.textContent = `Continued · ${session.turn_count} messages`;
    setViewingMode(false); // live mode — input is enabled
    renderConversationsList(_cachedSessions);
    scrollToBottom();
    userInput.focus();
}

function renderTurnsIntoChat(turns) {
    turns.forEach(turn => {
        // User bubble
        const uDiv = document.createElement("div");
        uDiv.className = "user-bubble";
        const tDiv = document.createElement("div");
        tDiv.style.cssText = "font-size:0.85rem;opacity:0.7;margin-bottom:4px;";
        tDiv.textContent = (turn.timestamp || "").slice(12, 17) || "";
        const uText = document.createElement("div");
        uText.textContent = turn.user;
        uDiv.appendChild(tDiv); uDiv.appendChild(uText);
        messagesInner.appendChild(uDiv);
        // Bot bubble
        const bDiv = document.createElement("div");
        bDiv.className = "bot-bubble";
        bDiv.innerHTML = formatBotContent(turn.assistant, turn.intent || null);
        messagesInner.appendChild(bDiv);
        // Rebuild LLM context for current session only
        if (turn.session_id === SESSION_ID) {
            history.push({ role: "user", content: turn.user });
            history.push({ role: "assistant", content: turn.assistant });
        }
    });
    if (history.length > MAX_HISTORY * 2) history = history.slice(-MAX_HISTORY * 2);
}

// ── Persist & Load History ───────────────────────────────────
async function fetchAndRenderSidebar() {
    try {
        const url = apiUrlInput.value.replace(/\/$/, "");
        // Fetch only THIS user's history (filtered by source IP on the server)
        const res = await fetch(`${url}/history/mine`);
        if (!res.ok) return;
        const data = await res.json();
        renderConversationsList(data.sessions || []);
    } catch (e) { console.warn("[History] sidebar fetch error:", e); }
}

async function persistTurn(userText, assistantText, intentStr) {
    try {
        const url = apiUrlInput.value.replace(/\/$/, "");
        const ts = new Date().toLocaleString("en-IN", {
            timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
        });
        await fetch(`${url}/history/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                user: userText, assistant: assistantText,
                timestamp: ts + " IST", intent: intentStr || null,
                session_id: SESSION_ID, user_agent: navigator.userAgent
            })
        });
        fetchAndRenderSidebar();
    } catch (e) { console.warn("[History] persist error:", e); }
}

// On page load: restore current session turns + load sidebar (MY history only)
window.addEventListener("load", async () => {
    setTimeout(async () => {
        try {
            const url = apiUrlInput.value.replace(/\/$/, "");
            // Use /history/mine so we only see sessions belonging to our IP
            const res = await fetch(`${url}/history/mine`);
            if (!res.ok) return;
            const data = await res.json();
            const sessions = data.sessions || [];
            renderConversationsList(sessions);
            // Auto-restore the most recent session for this browser (by stored SESSION_ID)
            const stored = sessions.find(s => s.session_id === SESSION_ID);
            const toRestore = stored || null; // only restore if session_id matches
            if (toRestore) {
                renderTurnsIntoChat(toRestore.turns);
                chatTitle.textContent = toRestore.turns[0]?.user || "STP Monitor Chatbot";
                scrollToBottom();
            }
        } catch (e) { console.warn("[History] init error:", e); }
    }, 500);
});

// ── Markdown renderer ────────────────────────────────────────
function renderMarkdown(text) {
    if (!text) return "";
    const lines = text.split('\n');
    const out = [];
    let i = 0;
    const isTableRow = l => l.trim().startsWith('|') && l.trim().endsWith('|');
    const isSepRow = l => /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(l);
    while (i < lines.length) {
        if (isTableRow(lines[i]) && i + 1 < lines.length && (isSepRow(lines[i + 1]) || isTableRow(lines[i + 1]))) {
            const tableLines = [];
            while (i < lines.length && (isTableRow(lines[i]) || isSepRow(lines[i]))) { tableLines.push(lines[i]); i++; }
            const parseRow = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
            const hCells = parseRow(tableLines[0]);
            const bRows = tableLines.slice(1).filter(l => !isSepRow(l)).map(parseRow);
            let tbl = '<div class="latex-table-wrap"><table class="latex-table"><thead><tr>';
            hCells.forEach(c => tbl += `<th>${inlineMarkdown(c)}</th>`);
            tbl += '</tr></thead><tbody>';
            bRows.forEach(r => { tbl += '<tr>'; r.forEach(c => tbl += `<td>${inlineMarkdown(c)}</td>`); tbl += '</tr>'; });
            tbl += '</tbody></table></div>';
            out.push(tbl);
        } else {
            out.push(inlineMarkdown(lines[i]) + (i < lines.length - 1 ? '<br/>' : ''));
            i++;
        }
    }
    return out.join('');
}

function inlineMarkdown(text) {
    if (!text) return '';
    return text
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/`(.*?)`/g, '<code style="background:var(--bg-300);padding:2px 4px;border-radius:4px;color:var(--accent-blue);font-family:var(--font-mono);font-size:12px;">$1</code>');
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
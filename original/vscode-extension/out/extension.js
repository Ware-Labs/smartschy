"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const fs = __importStar(require("fs/promises"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const schematicPanel_1 = require("./panels/schematicPanel");
const pythonRunner_1 = require("./python/pythonRunner");
const bomDatasheetMapper_1 = require("./state/bomDatasheetMapper");
const conversationStore_1 = require("./state/conversationStore");
const workflowState_1 = require("./state/workflowState");
const chatViewProvider_1 = require("./views/chatViewProvider");
const resourcesTreeProvider_1 = require("./views/resourcesTreeProvider");
let state = { ...workflowState_1.initialWorkflowState };
let runningIngest;
let runningAsk;
let activeConversation;
let lastQuestion = "";
let persistWorkspaceRoot = "";
function nowIso() {
    return new Date().toISOString();
}
function compactTimestamp(now = new Date()) {
    const yyyy = String(now.getFullYear());
    const mm = String(now.getMonth() + 1).padStart(2, "0");
    const dd = String(now.getDate()).padStart(2, "0");
    const hh = String(now.getHours()).padStart(2, "0");
    const mi = String(now.getMinutes()).padStart(2, "0");
    const ss = String(now.getSeconds()).padStart(2, "0");
    return `${yyyy}${mm}${dd}_${hh}${mi}${ss}`;
}
async function ensureMarkdownResponseFile(workspaceRoot, question, answerText, preferredPath) {
    if (preferredPath) {
        return preferredPath;
    }
    const responsesDir = path.join(workspaceRoot, "derived", "qa", "responses");
    await fs.mkdir(responsesDir, { recursive: true });
    const stamp = compactTimestamp();
    const mdPath = path.join(responsesDir, `response_${stamp}.md`);
    const body = [
        `# PCB QA Response (${stamp})`,
        "",
        "## Question",
        "",
        question,
        "",
        "## Answer",
        "",
        answerText,
        "",
    ].join("\n");
    await fs.writeFile(mdPath, body, "utf8");
    return mdPath;
}
async function fileExists(filePath) {
    if (!filePath) {
        return false;
    }
    try {
        await fs.access(filePath);
        return true;
    }
    catch {
        return false;
    }
}
async function buildResourceSignature(resources) {
    const files = [
        resources.dsnPath,
        resources.bomCsvPath,
        resources.schematicPdfPath,
        ...resources.datasheetMappings.map((row) => row.mappedPath),
    ]
        .filter((p) => Boolean(p))
        .sort((a, b) => a.localeCompare(b));
    const parts = [];
    for (const filePath of files) {
        try {
            const stat = await fs.stat(filePath);
            parts.push(`${filePath}|${stat.size}|${Math.trunc(stat.mtimeMs)}`);
        }
        catch {
            parts.push(`${filePath}|missing`);
        }
    }
    return parts.join("\n");
}
async function persistWorkflowState(workflow) {
    if (!persistWorkspaceRoot) {
        return;
    }
    const stateDir = (0, workflowState_1.getQaStateDir)(persistWorkspaceRoot);
    await fs.mkdir(stateDir, { recursive: true });
    const statePath = path.join(stateDir, "workflow_state.json");
    await fs.writeFile(statePath, JSON.stringify(workflow, null, 2), "utf8");
}
async function loadPersistedWorkflowState(workspaceRoot) {
    const statePath = path.join((0, workflowState_1.getQaStateDir)(workspaceRoot), "workflow_state.json");
    try {
        const raw = await fs.readFile(statePath, "utf8");
        const parsed = JSON.parse(raw);
        const persistedResources = parsed.resources ?? workflowState_1.initialWorkflowState.resources;
        const resources = {
            dsnPath: (await fileExists(persistedResources.dsnPath)) ? persistedResources.dsnPath : undefined,
            bomCsvPath: (await fileExists(persistedResources.bomCsvPath)) ? persistedResources.bomCsvPath : undefined,
            schematicPdfPath: (await fileExists(persistedResources.schematicPdfPath)) ? persistedResources.schematicPdfPath : undefined,
            resourcesDir: persistedResources.resourcesDir,
            datasheetMappings: [],
            unmatchedParts: persistedResources.unmatchedParts ?? [],
        };
        for (const mapping of persistedResources.datasheetMappings ?? []) {
            if (await fileExists(mapping.mappedPath)) {
                resources.datasheetMappings.push(mapping);
            }
        }
        const currentSignature = await buildResourceSignature(resources);
        const status = parsed.lastIngestSignature && parsed.lastIngestSignature === currentSignature
            ? "READY_FOR_CHAT"
            : (0, workflowState_1.nextStatusFromResources)(resources);
        return {
            ...parsed,
            status,
            resources,
            lastError: undefined,
        };
    }
    catch {
        return undefined;
    }
}
function summarizeConversation(messages) {
    const userLine = messages.find((msg) => msg.role === "user")?.content ?? "No question";
    const assistantLine = messages.filter((msg) => msg.role === "assistant").at(-1)?.content ?? "No answer yet";
    const trimmedAssistant = assistantLine.length > 120 ? `${assistantLine.slice(0, 117)}...` : assistantLine;
    return `Q: ${userLine} | A: ${trimmedAssistant}`;
}
function normalizeState(resources) {
    return {
        ...state,
        resources,
        status: (0, workflowState_1.nextStatusFromResources)(resources),
        ingestSummaryPath: undefined,
        lastError: undefined,
        lastIngestSignature: undefined,
        lastIngestAt: undefined,
    };
}
function applyState(next, resourcesView, chatView) {
    state = next;
    resourcesView.updateState(state);
    chatView.updateWorkflowState(state);
    void persistWorkflowState(state);
}
async function pickSingleFile(filters) {
    const selected = await vscode.window.showOpenDialog({
        canSelectFiles: true,
        canSelectFolders: false,
        canSelectMany: false,
        filters,
    });
    return selected?.[0]?.fsPath;
}
async function listPdfFiles(dirPath) {
    try {
        const names = await fs.readdir(dirPath);
        return names
            .filter((name) => name.toLowerCase().endsWith(".pdf"))
            .map((name) => path.join(dirPath, name));
    }
    catch {
        return [];
    }
}
function appendStatus(chatView, content) {
    chatView.addMessage({ role: "status", content, timestamp: nowIso() });
}
function appendChat(chatView, role, content) {
    chatView.addMessage({ role, content, timestamp: nowIso() });
}
function createConversation() {
    const id = String(Date.now());
    return {
        id,
        title: `Conversation ${new Date().toLocaleString()}`,
        createdAt: nowIso(),
        updatedAt: nowIso(),
        messages: [],
        summary: "No messages yet.",
    };
}
function withConversationMessage(conversation, msg) {
    const messages = [...conversation.messages, msg];
    return {
        ...conversation,
        messages,
        updatedAt: nowIso(),
        summary: summarizeConversation(messages),
    };
}
async function ensureConversation(store, chatView) {
    if (activeConversation) {
        return activeConversation;
    }
    const existing = await store.listConversations();
    activeConversation = existing[0] ?? createConversation();
    chatView.setConversation(activeConversation);
    return activeConversation;
}
function activate(context) {
    const workspaceRoot = (0, workflowState_1.getWorkspaceRoot)();
    persistWorkspaceRoot = workspaceRoot;
    const runner = new pythonRunner_1.PythonRunner({ cwd: workspaceRoot });
    const conversationStore = new conversationStore_1.ConversationStore(workspaceRoot);
    const resourcesView = new resourcesTreeProvider_1.ResourcesTreeProvider(state, workspaceRoot);
    const schematicPanel = new schematicPanel_1.SchematicPanel();
    const chatView = new chatViewProvider_1.ChatViewProvider(context.extensionUri, state, (cmd) => {
        if (cmd.type === "ask") {
            void runAgentAsk(cmd.question, cmd.mode);
        }
        else if (cmd.type === "jumpToPage") {
            schematicPanel.jumpToPage(cmd.pageNumber);
            schematicPanel.show(context.extensionUri, state.resources.schematicPdfPath);
        }
        else if (cmd.type === "cancelAsk") {
            runningAsk?.cancel();
        }
    });
    context.subscriptions.push(vscode.window.registerTreeDataProvider("smartschy.resourcesView", resourcesView), vscode.window.registerWebviewViewProvider(chatViewProvider_1.ChatViewProvider.viewType, chatView), vscode.commands.registerCommand("smartschy.openSchematicViewer", () => {
        schematicPanel.show(context.extensionUri, state.resources.schematicPdfPath);
    }), vscode.commands.registerCommand("smartschy.addDsn", async () => {
        const dsnPath = await pickSingleFile({ DSN: ["dsn"] });
        if (!dsnPath) {
            return;
        }
        applyState(normalizeState({ ...state.resources, dsnPath }), resourcesView, chatView);
    }), vscode.commands.registerCommand("smartschy.addBom", async () => {
        const bomCsvPath = await pickSingleFile({ CSV: ["csv"] });
        if (!bomCsvPath) {
            return;
        }
        applyState(normalizeState({ ...state.resources, bomCsvPath }), resourcesView, chatView);
    }), vscode.commands.registerCommand("smartschy.addSchematic", async () => {
        const schematicPdfPath = await pickSingleFile({ PDF: ["pdf"] });
        if (!schematicPdfPath) {
            return;
        }
        applyState(normalizeState({ ...state.resources, schematicPdfPath }), resourcesView, chatView);
        schematicPanel.show(context.extensionUri, schematicPdfPath);
    }), vscode.commands.registerCommand("smartschy.addDatasheets", async () => {
        if (!state.resources.bomCsvPath) {
            vscode.window.showWarningMessage("Add BOM first so datasheets can map by part number.");
            return;
        }
        const selected = await vscode.window.showOpenDialog({
            canSelectFiles: true,
            canSelectFolders: false,
            canSelectMany: true,
            filters: { PDF: ["pdf"] },
        });
        if (!selected || selected.length === 0) {
            return;
        }
        const resourcesDir = path.join(workspaceRoot, ".smartschy", "resources");
        const existingPdfFiles = await listPdfFiles(resourcesDir);
        const allDatasheets = [...new Set([...existingPdfFiles, ...selected.map((item) => item.fsPath)])];
        const mappingResult = await (0, bomDatasheetMapper_1.mapDatasheetsFromBom)(state.resources.bomCsvPath, allDatasheets, resourcesDir);
        const mappings = mappingResult.mappings;
        const status = mappings.length > 0 ? "DATASHEETS_MAPPED" : "FILES_SELECTED";
        applyState({
            ...state,
            status,
            ingestSummaryPath: undefined,
            resources: {
                ...state.resources,
                resourcesDir,
                datasheetMappings: mappings,
                unmatchedParts: mappingResult.unmatchedParts,
            },
            lastError: undefined,
            lastIngestSignature: undefined,
            lastIngestAt: undefined,
        }, resourcesView, chatView);
        if (mappings.length === 0) {
            vscode.window.showWarningMessage("No datasheets matched BOM part numbers.");
        }
        else {
            const totalParts = (await (0, bomDatasheetMapper_1.parseBomPartRows)(state.resources.bomCsvPath)).length;
            const mappedCount = mappings.length;
            vscode.window.showInformationMessage(`Mapped ${mappedCount} datasheets. ${Math.max(totalParts - mappedCount, 0)} part numbers remain unmapped.`);
        }
    }), vscode.commands.registerCommand("smartschy.mapUnmatchedPart", async (part) => {
        if (!state.resources.bomCsvPath) {
            vscode.window.showWarningMessage("Add a BOM first.");
            return;
        }
        const resourcesDir = state.resources.resourcesDir ?? path.join(workspaceRoot, ".smartschy", "resources");
        const selectedPath = await pickSingleFile({ PDF: ["pdf"] });
        if (!selectedPath) {
            return;
        }
        const safePartNumber = part.partNumber.trim().replace(/[\\/:*?"<>|]/g, "_");
        const targetPath = path.join(resourcesDir, `${safePartNumber}.pdf`);
        await fs.mkdir(resourcesDir, { recursive: true });
        await fs.copyFile(selectedPath, targetPath);
        const allDatasheets = await listPdfFiles(resourcesDir);
        const remap = await (0, bomDatasheetMapper_1.mapDatasheetsFromBom)(state.resources.bomCsvPath, allDatasheets, resourcesDir);
        applyState({
            ...state,
            status: remap.mappings.length > 0 ? "DATASHEETS_MAPPED" : "FILES_SELECTED",
            ingestSummaryPath: undefined,
            resources: {
                ...state.resources,
                resourcesDir,
                datasheetMappings: remap.mappings,
                unmatchedParts: remap.unmatchedParts,
            },
            lastError: undefined,
            lastIngestSignature: undefined,
            lastIngestAt: undefined,
        }, resourcesView, chatView);
        vscode.window.showInformationMessage(`Mapped ${part.partNumber} to ${path.basename(selectedPath)}.`);
    }), vscode.commands.registerCommand("smartschy.runIngest", async () => {
        await runIngest();
    }), vscode.commands.registerCommand("smartschy.retryIngest", async () => {
        await runIngest();
    }), vscode.commands.registerCommand("smartschy.cancelIngest", () => {
        runningIngest?.cancel();
        appendStatus(chatView, "Ingest cancel requested.");
    }), vscode.commands.registerCommand("smartschy.cancelAsk", () => {
        runningAsk?.cancel();
        appendStatus(chatView, "Chat request cancel requested.");
    }), vscode.commands.registerCommand("smartschy.retryAsk", async () => {
        if (!lastQuestion) {
            vscode.window.showInformationMessage("No previous chat question to retry.");
            return;
        }
        await runAgentAsk(lastQuestion, "auto");
    }), vscode.commands.registerCommand("smartschy.resetWorkflow", () => {
        runningIngest?.cancel();
        runningAsk?.cancel();
        activeConversation = undefined;
        lastQuestion = "";
        applyState({ ...workflowState_1.initialWorkflowState }, resourcesView, chatView);
        chatView.setMessages([]);
    }));
    void (async () => {
        const restored = await loadPersistedWorkflowState(workspaceRoot);
        if (restored) {
            applyState(restored, resourcesView, chatView);
        }
        const existing = await conversationStore.listConversations();
        activeConversation = existing[0] ?? createConversation();
        chatView.setConversation(activeConversation);
    })();
    async function runIngest() {
        if (runningIngest) {
            vscode.window.showInformationMessage("Ingest is already running.");
            return;
        }
        const { dsnPath, bomCsvPath, schematicPdfPath, resourcesDir } = state.resources;
        if (!(dsnPath && bomCsvPath && schematicPdfPath && resourcesDir)) {
            vscode.window.showWarningMessage("Add DSN, BOM, Schematic, and mapped datasheets before ingest.");
            return;
        }
        applyState({ ...state, status: "INGESTING", lastError: undefined }, resourcesView, chatView);
        appendStatus(chatView, "Ingest started...");
        let ingestError = "";
        const cmd = runner.runModule([
            "-m",
            "pcb_qa.cli",
            "ingest",
            "--project-root",
            workspaceRoot,
            "--dsn-path",
            dsnPath,
            "--bom-csv-path",
            bomCsvPath,
            "--schematic-pdf",
            schematicPdfPath,
            "--resources-dir",
            resourcesDir,
            "--llm-enrich",
        ], {
            onStdout: (line) => appendStatus(chatView, line),
            onStderr: (line) => {
                ingestError = line;
                appendStatus(chatView, line);
            },
        });
        runningIngest = cmd;
        const result = await cmd.waitForExit();
        runningIngest = undefined;
        if (result.exitCode !== 0) {
            const errorMessage = ingestError || "Ingest command failed.";
            applyState({ ...state, status: "ERROR", lastError: errorMessage }, resourcesView, chatView);
            appendStatus(chatView, `Ingest failed: ${errorMessage}`);
            void vscode.window.showErrorMessage(`PCB QA ingest failed: ${errorMessage}`);
            return;
        }
        const ingestSummaryPath = path.join(workspaceRoot, "derived", "ingest_summary.json");
        const lastIngestSignature = await buildResourceSignature(state.resources);
        applyState({
            ...state,
            status: "READY_FOR_CHAT",
            ingestSummaryPath,
            lastError: undefined,
            lastIngestSignature,
            lastIngestAt: nowIso(),
        }, resourcesView, chatView);
        appendStatus(chatView, "Ingest complete. Chat is now enabled.");
    }
    async function runAgentAsk(question, mode = "auto") {
        if (runningAsk) {
            vscode.window.showInformationMessage("A chat request is already in progress.");
            return;
        }
        if (state.status !== "READY_FOR_CHAT") {
            vscode.window.showWarningMessage("Run ingest successfully before asking questions.");
            return;
        }
        lastQuestion = question;
        const conversation = await ensureConversation(conversationStore, chatView);
        const userMsg = { role: "user", content: question, timestamp: nowIso() };
        activeConversation = withConversationMessage(conversation, userMsg);
        chatView.setConversation(activeConversation);
        await conversationStore.upsertConversation(activeConversation);
        const priorContext = await conversationStore.buildCarryoverContext(activeConversation.id, 3);
        const contextualQuestion = priorContext.length
            ? `${question}\n\nPrevious conversation context:\n- ${priorContext.join("\n- ")}`
            : question;
        appendStatus(chatView, "Submitting agent-ask request...");
        const stdoutLines = [];
        let lastAnswerPath = "";
        let lastMarkdownPath = "";
        let askError = "";
        const cmd = runner.runModule([
            "-m",
            "pcb_qa.cli",
            "agent-ask",
            "--project-root",
            workspaceRoot,
            "--question",
            contextualQuestion,
            "--mode",
            mode,
            "--answer-with-llm",
            "--model",
            "gpt-5",
            "--image-detail",
            "high",
        ], {
            onStdout: (line) => {
                stdoutLines.push(line);
            },
            onStderr: (line) => {
                askError = line;
                appendStatus(chatView, line);
                if (line.includes("agent_answer.txt")) {
                    lastAnswerPath = path.join(workspaceRoot, "derived", "qa", "agent_answer.txt");
                }
            },
        });
        runningAsk = cmd;
        const result = await cmd.waitForExit();
        runningAsk = undefined;
        if (result.exitCode !== 0) {
            const errorMessage = askError || "agent-ask failed.";
            appendStatus(chatView, `agent-ask failed: ${errorMessage}`);
            applyState({ ...state, lastError: errorMessage, status: "READY_FOR_CHAT" }, resourcesView, chatView);
            void vscode.window.showErrorMessage(`PCB QA chat request failed: ${errorMessage}`);
            return;
        }
        let assistantContent = "Completed request.";
        const rawStdout = stdoutLines.join("\n");
        let payload = {};
        try {
            payload = JSON.parse(rawStdout);
        }
        catch {
            payload = {};
        }
        if (payload.llm_answer?.answer_path) {
            lastAnswerPath = payload.llm_answer.answer_path;
        }
        if (payload.llm_answer?.markdown_answer_path) {
            lastMarkdownPath = payload.llm_answer.markdown_answer_path;
        }
        if (lastAnswerPath) {
            try {
                assistantContent = await fs.readFile(lastAnswerPath, "utf8");
            }
            catch {
                assistantContent = "Completed request but could not read the final answer file.";
            }
        }
        else if (typeof payload.answer_text === "string" && payload.answer_text.trim().length > 0) {
            assistantContent = payload.answer_text.trim();
            if (payload.suggested_precision_followup) {
                assistantContent += `\n\n${payload.suggested_precision_followup}`;
            }
        }
        else {
            assistantContent = `Completed with stop reason: ${payload.stop_reason ?? "unknown"}`;
        }
        if (payload.route_decision?.route) {
            appendStatus(chatView, `Route: ${payload.route_decision.route}`);
        }
        appendChat(chatView, "assistant", assistantContent);
        const updatedConversation = withConversationMessage(activeConversation, {
            role: "assistant",
            content: assistantContent,
            timestamp: nowIso(),
        });
        activeConversation = updatedConversation;
        chatView.setConversation(updatedConversation);
        await conversationStore.upsertConversation(updatedConversation);
        const ensuredMarkdownPath = await ensureMarkdownResponseFile(workspaceRoot, question, assistantContent, lastMarkdownPath);
        resourcesView.updateState(state);
        const markdownUri = vscode.Uri.file(ensuredMarkdownPath);
        try {
            await vscode.commands.executeCommand("markdown.showPreview", markdownUri);
        }
        catch {
            await vscode.commands.executeCommand("vscode.open", markdownUri);
        }
        appendStatus(chatView, `Opened markdown response: ${path.basename(ensuredMarkdownPath)}`);
    }
}
function deactivate() {
    runningIngest?.cancel();
    runningAsk?.cancel();
}
//# sourceMappingURL=extension.js.map
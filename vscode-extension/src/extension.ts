import * as fs from "fs/promises";
import * as path from "path";
import * as vscode from "vscode";
import { SchematicPanel } from "./panels/schematicPanel";
import { PythonRunner, RunningCommand } from "./python/pythonRunner";
import { mapDatasheetsFromBom, parseBomPartRows } from "./state/bomDatasheetMapper";
import { ChatMessage, Conversation, ConversationStore } from "./state/conversationStore";
import {
  WorkflowResources,
  WorkflowState,
  UnmatchedPart,
  getQaStateDir,
  getWorkspaceRoot,
  initialWorkflowState,
  nextStatusFromResources,
} from "./state/workflowState";
import { ChatViewProvider } from "./views/chatViewProvider";
import { ResourcesTreeProvider } from "./views/resourcesTreeProvider";

let state: WorkflowState = { ...initialWorkflowState };
let runningIngest: RunningCommand | undefined;
let runningAsk: RunningCommand | undefined;
let activeConversation: Conversation | undefined;
let lastQuestion = "";
let persistWorkspaceRoot = "";

function nowIso(): string {
  return new Date().toISOString();
}

function compactTimestamp(now: Date = new Date()): string {
  const yyyy = String(now.getFullYear());
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  const hh = String(now.getHours()).padStart(2, "0");
  const mi = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");
  return `${yyyy}${mm}${dd}_${hh}${mi}${ss}`;
}

async function ensureMarkdownResponseFile(
  workspaceRoot: string,
  question: string,
  answerText: string,
  preferredPath?: string
): Promise<string> {
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

async function fileExists(filePath?: string): Promise<boolean> {
  if (!filePath) {
    return false;
  }
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function buildResourceSignature(resources: WorkflowResources): Promise<string> {
  const files = [
    resources.dsnPath,
    resources.bomCsvPath,
    resources.schematicPdfPath,
    ...resources.datasheetMappings.map((row) => row.mappedPath),
  ]
    .filter((p): p is string => Boolean(p))
    .sort((a, b) => a.localeCompare(b));

  const parts: string[] = [];
  for (const filePath of files) {
    try {
      const stat = await fs.stat(filePath);
      parts.push(`${filePath}|${stat.size}|${Math.trunc(stat.mtimeMs)}`);
    } catch {
      parts.push(`${filePath}|missing`);
    }
  }
  return parts.join("\n");
}

async function persistWorkflowState(workflow: WorkflowState): Promise<void> {
  if (!persistWorkspaceRoot) {
    return;
  }
  const stateDir = getQaStateDir(persistWorkspaceRoot);
  await fs.mkdir(stateDir, { recursive: true });
  const statePath = path.join(stateDir, "workflow_state.json");
  await fs.writeFile(statePath, JSON.stringify(workflow, null, 2), "utf8");
}

async function loadPersistedWorkflowState(workspaceRoot: string): Promise<WorkflowState | undefined> {
  const statePath = path.join(getQaStateDir(workspaceRoot), "workflow_state.json");
  try {
    const raw = await fs.readFile(statePath, "utf8");
    const parsed = JSON.parse(raw) as WorkflowState;
    const persistedResources = parsed.resources ?? initialWorkflowState.resources;
    const resources: WorkflowResources = {
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
    const status =
      parsed.lastIngestSignature && parsed.lastIngestSignature === currentSignature
        ? "READY_FOR_CHAT"
        : nextStatusFromResources(resources);
    return {
      ...parsed,
      status,
      resources,
      lastError: undefined,
    };
  } catch {
    return undefined;
  }
}

function summarizeConversation(messages: ChatMessage[]): string {
  const userLine = messages.find((msg) => msg.role === "user")?.content ?? "No question";
  const assistantLine = messages.filter((msg) => msg.role === "assistant").at(-1)?.content ?? "No answer yet";
  const trimmedAssistant = assistantLine.length > 120 ? `${assistantLine.slice(0, 117)}...` : assistantLine;
  return `Q: ${userLine} | A: ${trimmedAssistant}`;
}

function normalizeState(resources: WorkflowResources): WorkflowState {
  return {
    ...state,
    resources,
    status: nextStatusFromResources(resources),
    ingestSummaryPath: undefined,
    lastError: undefined,
    lastIngestSignature: undefined,
    lastIngestAt: undefined,
  };
}

function applyState(
  next: WorkflowState,
  resourcesView: ResourcesTreeProvider,
  chatView: ChatViewProvider
): void {
  state = next;
  resourcesView.updateState(state);
  chatView.updateWorkflowState(state);
  void persistWorkflowState(state);
}

async function pickSingleFile(filters: { [name: string]: string[] }): Promise<string | undefined> {
  const selected = await vscode.window.showOpenDialog({
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    filters,
  });
  return selected?.[0]?.fsPath;
}

async function listPdfFiles(dirPath: string): Promise<string[]> {
  try {
    const names = await fs.readdir(dirPath);
    return names
      .filter((name) => name.toLowerCase().endsWith(".pdf"))
      .map((name) => path.join(dirPath, name));
  } catch {
    return [];
  }
}

function appendStatus(chatView: ChatViewProvider, content: string): void {
  chatView.addMessage({ role: "status", content, timestamp: nowIso() });
}

function appendChat(chatView: ChatViewProvider, role: "user" | "assistant", content: string): void {
  chatView.addMessage({ role, content, timestamp: nowIso() });
}

function createConversation(): Conversation {
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

function withConversationMessage(conversation: Conversation, msg: ChatMessage): Conversation {
  const messages = [...conversation.messages, msg];
  return {
    ...conversation,
    messages,
    updatedAt: nowIso(),
    summary: summarizeConversation(messages),
  };
}

async function ensureConversation(store: ConversationStore, chatView: ChatViewProvider): Promise<Conversation> {
  if (activeConversation) {
    return activeConversation;
  }
  const existing = await store.listConversations();
  activeConversation = existing[0] ?? createConversation();
  chatView.setConversation(activeConversation);
  return activeConversation;
}

export function activate(context: vscode.ExtensionContext): void {
  const workspaceRoot = getWorkspaceRoot();
  persistWorkspaceRoot = workspaceRoot;
  const runner = new PythonRunner({ cwd: workspaceRoot });
  const conversationStore = new ConversationStore(workspaceRoot);
  const resourcesView = new ResourcesTreeProvider(state, workspaceRoot);
  const schematicPanel = new SchematicPanel();

  const chatView = new ChatViewProvider(context.extensionUri, state, (cmd) => {
    if (cmd.type === "ask") {
      void runAgentAsk(cmd.question, cmd.mode);
    } else if (cmd.type === "jumpToPage") {
      schematicPanel.jumpToPage(cmd.pageNumber);
      schematicPanel.show(context.extensionUri, state.resources.schematicPdfPath);
    } else if (cmd.type === "cancelAsk") {
      runningAsk?.cancel();
    }
  });

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("smartschy.resourcesView", resourcesView),
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatView),
    vscode.commands.registerCommand("smartschy.openSchematicViewer", () => {
      schematicPanel.show(context.extensionUri, state.resources.schematicPdfPath);
    }),
    vscode.commands.registerCommand("smartschy.addDsn", async () => {
      const dsnPath = await pickSingleFile({ DSN: ["dsn"] });
      if (!dsnPath) {
        return;
      }
      applyState(normalizeState({ ...state.resources, dsnPath }), resourcesView, chatView);
    }),
    vscode.commands.registerCommand("smartschy.addBom", async () => {
      const bomCsvPath = await pickSingleFile({ CSV: ["csv"] });
      if (!bomCsvPath) {
        return;
      }
      applyState(normalizeState({ ...state.resources, bomCsvPath }), resourcesView, chatView);
    }),
    vscode.commands.registerCommand("smartschy.addSchematic", async () => {
      const schematicPdfPath = await pickSingleFile({ PDF: ["pdf"] });
      if (!schematicPdfPath) {
        return;
      }
      applyState(normalizeState({ ...state.resources, schematicPdfPath }), resourcesView, chatView);
      schematicPanel.show(context.extensionUri, schematicPdfPath);
    }),
    vscode.commands.registerCommand("smartschy.addDatasheets", async () => {
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
      const mappingResult = await mapDatasheetsFromBom(
        state.resources.bomCsvPath,
        allDatasheets,
        resourcesDir
      );
      const mappings = mappingResult.mappings;
      const status = mappings.length > 0 ? "DATASHEETS_MAPPED" : "FILES_SELECTED";
      applyState(
        {
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
        },
        resourcesView,
        chatView
      );
      if (mappings.length === 0) {
        vscode.window.showWarningMessage("No datasheets matched BOM part numbers.");
      } else {
        const totalParts = (await parseBomPartRows(state.resources.bomCsvPath)).length;
        const mappedCount = mappings.length;
        vscode.window.showInformationMessage(
          `Mapped ${mappedCount} datasheets. ${Math.max(totalParts - mappedCount, 0)} part numbers remain unmapped.`
        );
      }
    }),
    vscode.commands.registerCommand("smartschy.mapUnmatchedPart", async (part: UnmatchedPart) => {
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
      const remap = await mapDatasheetsFromBom(state.resources.bomCsvPath, allDatasheets, resourcesDir);
      applyState(
        {
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
        },
        resourcesView,
        chatView
      );
      vscode.window.showInformationMessage(`Mapped ${part.partNumber} to ${path.basename(selectedPath)}.`);
    }),
    vscode.commands.registerCommand("smartschy.runIngest", async () => {
      await runIngest();
    }),
    vscode.commands.registerCommand("smartschy.retryIngest", async () => {
      await runIngest();
    }),
    vscode.commands.registerCommand("smartschy.cancelIngest", () => {
      runningIngest?.cancel();
      appendStatus(chatView, "Ingest cancel requested.");
    }),
    vscode.commands.registerCommand("smartschy.cancelAsk", () => {
      runningAsk?.cancel();
      appendStatus(chatView, "Chat request cancel requested.");
    }),
    vscode.commands.registerCommand("smartschy.retryAsk", async () => {
      if (!lastQuestion) {
        vscode.window.showInformationMessage("No previous chat question to retry.");
        return;
      }
      await runAgentAsk(lastQuestion, "auto");
    }),
    vscode.commands.registerCommand("smartschy.resetWorkflow", () => {
      runningIngest?.cancel();
      runningAsk?.cancel();
      activeConversation = undefined;
      lastQuestion = "";
      applyState({ ...initialWorkflowState }, resourcesView, chatView);
      chatView.setMessages([]);
    })
  );

  void (async () => {
    const restored = await loadPersistedWorkflowState(workspaceRoot);
    if (restored) {
      applyState(restored, resourcesView, chatView);
    }
    const existing = await conversationStore.listConversations();
    activeConversation = existing[0] ?? createConversation();
    chatView.setConversation(activeConversation);
  })();

  async function runIngest(): Promise<void> {
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
    const cmd = runner.runModule(
      [
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
      ],
      {
        onStdout: (line) => appendStatus(chatView, line),
        onStderr: (line) => {
          ingestError = line;
          appendStatus(chatView, line);
        },
      }
    );
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
    applyState(
      {
        ...state,
        status: "READY_FOR_CHAT",
        ingestSummaryPath,
        lastError: undefined,
        lastIngestSignature,
        lastIngestAt: nowIso(),
      },
      resourcesView,
      chatView
    );
    appendStatus(chatView, "Ingest complete. Chat is now enabled.");
  }

  async function runAgentAsk(question: string, mode: "auto" | "general" | "precision" = "auto"): Promise<void> {
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
    const userMsg: ChatMessage = { role: "user", content: question, timestamp: nowIso() };
    activeConversation = withConversationMessage(conversation, userMsg);
    chatView.setConversation(activeConversation);
    await conversationStore.upsertConversation(activeConversation);

    const priorContext = await conversationStore.buildCarryoverContext(activeConversation.id, 3);
    const contextualQuestion = priorContext.length
      ? `${question}\n\nPrevious conversation context:\n- ${priorContext.join("\n- ")}`
      : question;
    appendStatus(chatView, "Submitting agent-ask request...");

    const stdoutLines: string[] = [];
    let lastAnswerPath = "";
    let lastMarkdownPath = "";
    let askError = "";
    const cmd = runner.runModule(
      [
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
      ],
      {
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
      }
    );
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
    let payload: {
      llm_answer?: { answer_path?: string; markdown_answer_path?: string };
      stop_reason?: string;
      route_decision?: { route?: string };
      answer_text?: string;
      suggested_precision_followup?: string;
    } = {};
    try {
      payload = JSON.parse(rawStdout) as { llm_answer?: { answer_path?: string; markdown_answer_path?: string }; stop_reason?: string };
    } catch {
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
      } catch {
        assistantContent = "Completed request but could not read the final answer file.";
      }
    } else if (typeof payload.answer_text === "string" && payload.answer_text.trim().length > 0) {
      assistantContent = payload.answer_text.trim();
      if (payload.suggested_precision_followup) {
        assistantContent += `\n\n${payload.suggested_precision_followup}`;
      }
    } else {
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
    const ensuredMarkdownPath = await ensureMarkdownResponseFile(
      workspaceRoot,
      question,
      assistantContent,
      lastMarkdownPath
    );
    resourcesView.updateState(state);
    const markdownUri = vscode.Uri.file(ensuredMarkdownPath);
    try {
      await vscode.commands.executeCommand("markdown.showPreview", markdownUri);
    } catch {
      await vscode.commands.executeCommand("vscode.open", markdownUri);
    }
    appendStatus(chatView, `Opened markdown response: ${path.basename(ensuredMarkdownPath)}`);
  }
}

export function deactivate(): void {
  runningIngest?.cancel();
  runningAsk?.cancel();
}


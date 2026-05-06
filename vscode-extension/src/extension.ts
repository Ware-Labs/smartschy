import * as fs from "fs/promises";
import * as path from "path";
import * as vscode from "vscode";
import { SchematicPanel } from "./panels/schematicPanel";
import { PythonRunner, RunningCommand } from "./python/pythonRunner";
import { mapDatasheetsFromBom } from "./state/bomDatasheetMapper";
import { ChatMessage, Conversation, ConversationStore } from "./state/conversationStore";
import {
  WorkflowResources,
  WorkflowState,
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

function nowIso(): string {
  return new Date().toISOString();
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
    lastError: undefined,
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
  const runner = new PythonRunner({ cwd: workspaceRoot });
  const conversationStore = new ConversationStore(workspaceRoot);
  const resourcesView = new ResourcesTreeProvider(state);
  const schematicPanel = new SchematicPanel();

  const chatView = new ChatViewProvider(context.extensionUri, state, (cmd) => {
    if (cmd.type === "ask") {
      void runAgentAsk(cmd.question);
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
      const mappings = await mapDatasheetsFromBom(
        state.resources.bomCsvPath,
        selected.map((item) => item.fsPath),
        resourcesDir
      );
      const status = mappings.length > 0 ? "DATASHEETS_MAPPED" : "FILES_SELECTED";
      applyState(
        {
          ...state,
          status,
          resources: { ...state.resources, resourcesDir, datasheetMappings: mappings },
          lastError: undefined,
        },
        resourcesView,
        chatView
      );
      if (mappings.length === 0) {
        vscode.window.showWarningMessage("No datasheets matched BOM part numbers.");
      } else {
        vscode.window.showInformationMessage(`Mapped ${mappings.length} datasheets from BOM part numbers.`);
      }
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
      await runAgentAsk(lastQuestion);
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
    applyState(
      { ...state, status: "READY_FOR_CHAT", ingestSummaryPath, lastError: undefined },
      resourcesView,
      chatView
    );
    appendStatus(chatView, "Ingest complete. Chat is now enabled.");
  }

  async function runAgentAsk(question: string): Promise<void> {
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

    let lastStdoutJson = "";
    let lastAnswerPath = "";
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
        "--answer-with-llm",
        "--model",
        "gpt-5",
        "--image-detail",
        "high",
      ],
      {
        onStdout: (line) => {
          lastStdoutJson = line;
          appendStatus(chatView, line);
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
    if (lastAnswerPath) {
      try {
        assistantContent = await fs.readFile(lastAnswerPath, "utf8");
      } catch {
        assistantContent = "Completed request but could not read the final answer file.";
      }
    } else {
      try {
        const payload = JSON.parse(lastStdoutJson) as { answer_path?: string; stop_reason?: string };
        if (payload.answer_path) {
          assistantContent = await fs.readFile(payload.answer_path, "utf8");
        } else {
          assistantContent = `Completed with stop reason: ${payload.stop_reason ?? "unknown"}`;
        }
      } catch {
        assistantContent = lastStdoutJson || assistantContent;
      }
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
  }
}

export function deactivate(): void {
  runningIngest?.cancel();
  runningAsk?.cancel();
}


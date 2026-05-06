import * as vscode from "vscode";
import { ChatMessage, Conversation } from "../state/conversationStore";
import { WorkflowState } from "../state/workflowState";

type ChatCommand = {
  type: "ask";
  question: string;
};

type ChatPageAction =
  | { type: "jumpToPage"; pageNumber: number }
  | { type: "cancelAsk" };

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "smartschy.chatView";

  private view?: vscode.WebviewView;
  private state: WorkflowState;
  private messages: ChatMessage[] = [];
  private currentConversation?: Conversation;

  public constructor(
    private readonly extensionUri: vscode.Uri,
    initialState: WorkflowState,
    private readonly onCommand: (cmd: ChatCommand | ChatPageAction) => void
  ) {
    this.state = initialState;
  }

  public resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this.renderHtml(webviewView.webview);
    webviewView.webview.onDidReceiveMessage((msg) => {
      if (msg?.type === "ask" && typeof msg.question === "string") {
        this.onCommand({ type: "ask", question: msg.question.trim() });
      } else if (msg?.type === "jumpToPage" && Number.isFinite(msg.pageNumber)) {
        this.onCommand({ type: "jumpToPage", pageNumber: Number(msg.pageNumber) });
      } else if (msg?.type === "cancelAsk") {
        this.onCommand({ type: "cancelAsk" });
      }
    });
    this.pushState();
  }

  public updateWorkflowState(state: WorkflowState): void {
    this.state = state;
    this.pushState();
  }

  public setConversation(conversation: Conversation): void {
    this.currentConversation = conversation;
    this.messages = conversation.messages;
    this.pushState();
  }

  public addMessage(message: ChatMessage): void {
    this.messages = [...this.messages, message];
    this.pushState();
  }

  public setMessages(messages: ChatMessage[]): void {
    this.messages = messages;
    this.pushState();
  }

  private pushState(): void {
    this.view?.webview.postMessage({
      type: "state",
      workflow: this.state,
      messages: this.messages,
      title: this.currentConversation?.title ?? "New conversation",
    });
  }

  private renderHtml(webview: vscode.Webview): string {
    const nonce = String(Date.now());
    return `<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
    <style>
      body { font-family: var(--vscode-font-family); margin: 0; padding: 0; color: var(--vscode-foreground); }
      #wrap { display: grid; grid-template-rows: auto 1fr auto; height: 100vh; gap: 8px; padding: 8px; box-sizing: border-box; }
      #status { font-size: 12px; color: var(--vscode-descriptionForeground); }
      #messages { overflow-y: auto; border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 8px; }
      .msg { margin-bottom: 8px; white-space: pre-wrap; }
      .role-user { color: var(--vscode-textLink-foreground); }
      .role-assistant { color: var(--vscode-foreground); }
      .role-status { color: var(--vscode-descriptionForeground); font-style: italic; }
      #composer { display: grid; grid-template-columns: 1fr auto auto; gap: 8px; }
      textarea { width: 100%; min-height: 60px; resize: vertical; }
      button { cursor: pointer; }
    </style>
  </head>
  <body>
    <div id="wrap">
      <div id="status"></div>
      <div id="messages"></div>
      <div id="composer">
        <textarea id="question" placeholder="Ask a question about the schematic..."></textarea>
        <button id="askBtn">Ask</button>
        <button id="cancelBtn">Cancel</button>
      </div>
    </div>
    <script nonce="${nonce}">
      const vscode = acquireVsCodeApi();
      const statusEl = document.getElementById("status");
      const messagesEl = document.getElementById("messages");
      const questionEl = document.getElementById("question");
      const askBtn = document.getElementById("askBtn");
      const cancelBtn = document.getElementById("cancelBtn");
      let workflow = { status: "EMPTY" };
      let messages = [];
      askBtn.addEventListener("click", () => {
        const question = questionEl.value.trim();
        if (!question) return;
        vscode.postMessage({ type: "ask", question });
        questionEl.value = "";
      });
      cancelBtn.addEventListener("click", () => vscode.postMessage({ type: "cancelAsk" }));
      window.addEventListener("message", (event) => {
        if (!event.data || event.data.type !== "state") return;
        workflow = event.data.workflow;
        messages = event.data.messages || [];
        const enabled = workflow.status === "READY_FOR_CHAT";
        questionEl.disabled = !enabled;
        askBtn.disabled = !enabled;
        statusEl.textContent = "Workflow: " + workflow.status + (workflow.lastError ? " | Error: " + workflow.lastError : "");
        messagesEl.innerHTML = messages.map((msg) => {
          const content = (msg.content || "").replace(/</g, "&lt;");
          const pageMatch = content.match(/page\\s+(\\d+)/i);
          const jumpButton = pageMatch ? '<button data-page="' + pageMatch[1] + '">Jump to page ' + pageMatch[1] + '</button>' : "";
          return '<div class="msg role-' + msg.role + '"><strong>' + msg.role + ':</strong> ' + content + ' ' + jumpButton + '</div>';
        }).join("");
        for (const button of messagesEl.querySelectorAll("button[data-page]")) {
          button.addEventListener("click", () => {
            const page = Number(button.getAttribute("data-page"));
            vscode.postMessage({ type: "jumpToPage", pageNumber: page });
          });
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      });
    </script>
  </body>
</html>`;
  }
}


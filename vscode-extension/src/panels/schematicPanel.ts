import * as vscode from "vscode";

export class SchematicPanel {
  private panel?: vscode.WebviewPanel;
  private schematicPath?: string;
  private highlightedPage?: number;

  public show(extensionUri: vscode.Uri, schematicPath?: string): void {
    this.schematicPath = schematicPath ?? this.schematicPath;
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel(
        "smartschy.schematicViewer",
        "Schematic Viewer",
        vscode.ViewColumn.One,
        { enableScripts: true, retainContextWhenHidden: true }
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
      });
      this.panel.webview.onDidReceiveMessage((msg) => {
        if (msg?.type === "openExternalPdf" && typeof msg.path === "string") {
          void vscode.commands.executeCommand("vscode.open", vscode.Uri.file(msg.path));
        }
      });
    }
    this.panel.reveal(vscode.ViewColumn.One);
    this.panel.webview.html = this.renderHtml(extensionUri, this.schematicPath, this.highlightedPage);
  }

  public jumpToPage(pageNumber: number): void {
    this.highlightedPage = pageNumber;
    if (this.panel) {
      this.panel.webview.postMessage({ type: "jumpToPage", pageNumber });
    }
  }

  private renderHtml(extensionUri: vscode.Uri, schematicPath?: string, highlightedPage?: number): string {
    const nonce = String(Date.now());
    const localPdf = schematicPath
      ? this.panel?.webview.asWebviewUri(vscode.Uri.file(schematicPath)).toString() ?? ""
      : "";
    const openHint = schematicPath ?? "No schematic selected";
    const pageQuery = highlightedPage ? `#page=${highlightedPage}` : "";
    const title = `Schematic: ${openHint}`;
    const details = localPdf
      ? `<iframe src="${localPdf}${pageQuery}" style="width:100%;height:100%;border:none;"></iframe>`
      : `<div style="padding:16px;">Add a schematic in the Resources panel, then reopen this viewer.</div>`;
    return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; frame-src ${this.panel?.webview.cspSource}; script-src 'nonce-${nonce}';" />
    <style>
      html, body { width: 100%; height: 100%; margin: 0; padding: 0; color: var(--vscode-foreground); background: var(--vscode-editor-background); }
      #root { display: grid; grid-template-rows: auto 1fr; height: 100%; }
      #header { font-size: 12px; padding: 8px; border-bottom: 1px solid var(--vscode-panel-border); }
      #content { min-height: 0; }
    </style>
  </head>
  <body>
    <div id="root">
      <div id="header">${title}</div>
      <div id="content">${details}</div>
    </div>
    <script nonce="${nonce}">
      window.addEventListener("message", (event) => {
        const msg = event.data;
        if (msg?.type === "jumpToPage" && Number.isFinite(msg.pageNumber)) {
          const iframe = document.querySelector("iframe");
          if (!iframe) return;
          const src = iframe.getAttribute("src") || "";
          const normalized = src.replace(/#page=\\d+$/, "");
          iframe.setAttribute("src", normalized + "#page=" + msg.pageNumber);
        }
      });
    </script>
  </body>
</html>`;
  }
}


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
exports.SchematicPanel = void 0;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
class SchematicPanel {
    constructor() {
        this.pageImageUris = [];
    }
    show(extensionUri, schematicPath) {
        this.schematicPath = schematicPath ?? this.schematicPath;
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const localRoots = [extensionUri];
        if (workspaceRoot) {
            localRoots.push(vscode.Uri.file(workspaceRoot));
        }
        if (!this.panel) {
            this.panel = vscode.window.createWebviewPanel("smartschy.schematicViewer", "Schematic Viewer", vscode.ViewColumn.One, {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: localRoots,
            });
            this.panel.onDidDispose(() => {
                this.panel = undefined;
                this.pageImageUris = [];
            });
            this.panel.webview.onDidReceiveMessage((msg) => {
                if (msg?.type === "openExternalPdf" && typeof msg.path === "string") {
                    void vscode.commands.executeCommand("vscode.open", vscode.Uri.file(msg.path));
                }
                else if (msg?.type === "jumpToPage" && Number.isFinite(msg.pageNumber)) {
                    this.jumpToPage(Number(msg.pageNumber));
                    this.panel?.webview.postMessage({ type: "jumpToPage", pageNumber: Number(msg.pageNumber) });
                }
            });
        }
        this.pageImageUris = this.resolveRenderedPageUris();
        this.panel.reveal(vscode.ViewColumn.One);
        this.panel.webview.html = this.renderHtml(extensionUri, this.schematicPath, this.highlightedPage);
    }
    jumpToPage(pageNumber) {
        this.highlightedPage = pageNumber;
        if (this.panel) {
            this.panel.webview.postMessage({ type: "jumpToPage", pageNumber });
        }
    }
    renderHtml(extensionUri, schematicPath, highlightedPage) {
        const nonce = String(Date.now());
        const openHint = schematicPath ?? "No schematic selected";
        const title = `Schematic: ${openHint}`;
        const initialPage = highlightedPage ?? this.pageImageUris[0]?.pageNumber ?? 1;
        const pageMapJson = JSON.stringify(this.pageImageUris);
        const details = schematicPath
            ? `
      <div style="display:flex;gap:8px;align-items:center;padding:8px;border-bottom:1px solid var(--vscode-panel-border);">
        <button id="prevBtn">Prev</button>
        <button id="nextBtn">Next</button>
        <label>Page <input id="pageInput" type="number" min="1" style="width:80px;" /></label>
        <button id="openPdfBtn">Open PDF</button>
        <span id="viewerStatus" style="font-size:12px;color:var(--vscode-descriptionForeground);"></span>
      </div>
      <div style="height:calc(100% - 44px);display:flex;justify-content:center;align-items:center;padding:8px;box-sizing:border-box;">
        <img id="pageImage" alt="Schematic page" style="width:100%;height:100%;object-fit:contain;border:1px solid var(--vscode-panel-border);background:var(--vscode-editor-background);" />
      </div>
      `
            : `<div style="padding:16px;">Add a schematic in the Resources panel, then reopen this viewer.</div>`;
        return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${this.panel?.webview.cspSource} blob: data:; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
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
      const vscode = acquireVsCodeApi();
      const pageMap = ${pageMapJson};
      let currentPage = ${initialPage};
      const pageInput = document.getElementById("pageInput");
      const pageImage = document.getElementById("pageImage");
      const viewerStatus = document.getElementById("viewerStatus");
      const openPdfBtn = document.getElementById("openPdfBtn");
      const prevBtn = document.getElementById("prevBtn");
      const nextBtn = document.getElementById("nextBtn");
      function renderPage(pageNumber) {
        if (!pageImage || !viewerStatus) return;
        const hit = pageMap.find((entry) => entry.pageNumber === pageNumber);
        if (!hit) {
          pageImage.removeAttribute("src");
          viewerStatus.textContent = "No rendered image for page " + pageNumber + ". Run ingest to generate page images.";
          return;
        }
        pageImage.setAttribute("src", hit.uri);
        viewerStatus.textContent = "Showing rendered page " + pageNumber;
        if (pageInput) pageInput.value = String(pageNumber);
      }
      if (prevBtn) {
        prevBtn.addEventListener("click", () => {
          currentPage = Math.max(1, currentPage - 1);
          renderPage(currentPage);
        });
      }
      if (nextBtn) {
        nextBtn.addEventListener("click", () => {
          currentPage += 1;
          renderPage(currentPage);
        });
      }
      if (pageInput) {
        pageInput.addEventListener("change", () => {
          const value = Number(pageInput.value);
          if (!Number.isFinite(value) || value < 1) return;
          currentPage = value;
          renderPage(currentPage);
        });
      }
      if (openPdfBtn) {
        openPdfBtn.addEventListener("click", () => {
          vscode.postMessage({ type: "openExternalPdf", path: ${JSON.stringify(schematicPath ?? "")} });
        });
      }
      renderPage(currentPage);
      window.addEventListener("message", (event) => {
        const msg = event.data;
        if (msg?.type === "jumpToPage" && Number.isFinite(msg.pageNumber)) {
          currentPage = Number(msg.pageNumber);
          renderPage(currentPage);
        }
      });
    </script>
  </body>
</html>`;
    }
    resolveRenderedPageUris() {
        if (!this.panel) {
            return [];
        }
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!workspaceRoot) {
            return [];
        }
        const manifestPath = path.join(workspaceRoot, "derived", "pdf", "schematic_page_images.json");
        if (!fs.existsSync(manifestPath)) {
            return [];
        }
        try {
            const payload = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
            const images = Array.isArray(payload.images) ? payload.images : [];
            return images
                .map((row) => {
                const imagePath = path.join(workspaceRoot, "derived", "pdf", row.image_path);
                return {
                    pageNumber: row.page_number,
                    uri: this.panel?.webview.asWebviewUri(vscode.Uri.file(imagePath)).toString() ?? "",
                };
            })
                .filter((row) => row.uri.length > 0);
        }
        catch {
            return [];
        }
    }
}
exports.SchematicPanel = SchematicPanel;
//# sourceMappingURL=schematicPanel.js.map
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
exports.ConversationStore = void 0;
const fs = __importStar(require("fs/promises"));
const path = __importStar(require("path"));
const workflowState_1 = require("./workflowState");
class ConversationStore {
    constructor(workspaceRoot) {
        this.workspaceRoot = workspaceRoot;
        this.storePath = path.join((0, workflowState_1.getQaStateDir)(workspaceRoot), "conversations.json");
    }
    async listConversations() {
        const payload = await this.readStore();
        return payload.conversations.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    }
    async getConversation(id) {
        const payload = await this.readStore();
        return payload.conversations.find((conv) => conv.id === id);
    }
    async upsertConversation(conversation) {
        const payload = await this.readStore();
        const idx = payload.conversations.findIndex((conv) => conv.id === conversation.id);
        if (idx >= 0) {
            payload.conversations[idx] = conversation;
        }
        else {
            payload.conversations.push(conversation);
        }
        await this.writeStore(payload);
    }
    async buildCarryoverContext(activeConversationId, limit = 3) {
        const payload = await this.readStore();
        const prior = payload.conversations
            .filter((conv) => conv.id !== activeConversationId)
            .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
            .slice(0, limit);
        return prior.map((conv) => `${conv.title}: ${conv.summary}`);
    }
    async readStore() {
        try {
            const raw = await fs.readFile(this.storePath, "utf8");
            const parsed = JSON.parse(raw);
            if (!Array.isArray(parsed.conversations)) {
                return { conversations: [] };
            }
            return parsed;
        }
        catch {
            return { conversations: [] };
        }
    }
    async writeStore(payload) {
        const dir = path.dirname(this.storePath);
        await fs.mkdir(dir, { recursive: true });
        await fs.writeFile(this.storePath, JSON.stringify(payload, null, 2), "utf8");
    }
}
exports.ConversationStore = ConversationStore;
//# sourceMappingURL=conversationStore.js.map
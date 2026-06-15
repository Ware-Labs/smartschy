import * as fs from "fs/promises";
import * as path from "path";
import { getQaStateDir } from "./workflowState";

export interface ChatMessage {
  role: "user" | "assistant" | "status";
  content: string;
  timestamp: string;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
  summary: string;
}

interface ConversationFile {
  conversations: Conversation[];
}

export class ConversationStore {
  private readonly workspaceRoot: string;
  private readonly storePath: string;

  public constructor(workspaceRoot: string) {
    this.workspaceRoot = workspaceRoot;
    this.storePath = path.join(getQaStateDir(workspaceRoot), "conversations.json");
  }

  public async listConversations(): Promise<Conversation[]> {
    const payload = await this.readStore();
    return payload.conversations.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }

  public async getConversation(id: string): Promise<Conversation | undefined> {
    const payload = await this.readStore();
    return payload.conversations.find((conv) => conv.id === id);
  }

  public async upsertConversation(conversation: Conversation): Promise<void> {
    const payload = await this.readStore();
    const idx = payload.conversations.findIndex((conv) => conv.id === conversation.id);
    if (idx >= 0) {
      payload.conversations[idx] = conversation;
    } else {
      payload.conversations.push(conversation);
    }
    await this.writeStore(payload);
  }

  public async buildCarryoverContext(activeConversationId: string, limit = 3): Promise<string[]> {
    const payload = await this.readStore();
    const prior = payload.conversations
      .filter((conv) => conv.id !== activeConversationId)
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
      .slice(0, limit);
    return prior.map((conv) => `${conv.title}: ${conv.summary}`);
  }

  private async readStore(): Promise<ConversationFile> {
    try {
      const raw = await fs.readFile(this.storePath, "utf8");
      const parsed = JSON.parse(raw) as ConversationFile;
      if (!Array.isArray(parsed.conversations)) {
        return { conversations: [] };
      }
      return parsed;
    } catch {
      return { conversations: [] };
    }
  }

  private async writeStore(payload: ConversationFile): Promise<void> {
    const dir = path.dirname(this.storePath);
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(this.storePath, JSON.stringify(payload, null, 2), "utf8");
  }
}


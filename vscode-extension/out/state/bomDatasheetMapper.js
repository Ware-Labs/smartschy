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
exports.parseBomPartRows = parseBomPartRows;
exports.mapDatasheetsFromBom = mapDatasheetsFromBom;
const fs = __importStar(require("fs/promises"));
const path = __importStar(require("path"));
function parseCsvLine(line) {
    const cells = [];
    let current = "";
    let inQuote = false;
    for (let i = 0; i < line.length; i += 1) {
        const ch = line[i];
        if (ch === '"') {
            if (inQuote && line[i + 1] === '"') {
                current += '"';
                i += 1;
            }
            else {
                inQuote = !inQuote;
            }
            continue;
        }
        if (ch === "," && !inQuote) {
            cells.push(current.trim());
            current = "";
            continue;
        }
        current += ch;
    }
    cells.push(current.trim());
    return cells;
}
function sanitizePartNumber(partNumber) {
    return partNumber.trim().replace(/[\\/:*?"<>|]/g, "_");
}
function normalizeToken(value) {
    return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}
function dedupeBomRows(rows) {
    const byPart = new Map();
    for (const row of rows) {
        const key = normalizeToken(row.partNumber);
        if (!key || byPart.has(key)) {
            continue;
        }
        byPart.set(key, row);
    }
    return [...byPart.values()];
}
async function copyDatasheetToPart(sourcePath, partNumber, resourcesDir) {
    const safePartNumber = sanitizePartNumber(partNumber);
    const mappedPath = path.join(resourcesDir, `${safePartNumber}.pdf`);
    if (path.resolve(sourcePath) !== path.resolve(mappedPath)) {
        await fs.copyFile(sourcePath, mappedPath);
    }
    return mappedPath;
}
async function parseBomPartRows(bomCsvPath) {
    const raw = await fs.readFile(bomCsvPath, "utf8");
    const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
    if (lines.length <= 1) {
        return [];
    }
    const headers = parseCsvLine(lines[0]);
    const manufacturerIdx = headers.findIndex((h) => h.trim().toLowerCase() === "manufacturer");
    const partNumberIdx = headers.findIndex((h) => h.trim().toLowerCase() === "part number");
    if (manufacturerIdx < 0 || partNumberIdx < 0) {
        return [];
    }
    const rows = [];
    for (let i = 1; i < lines.length; i += 1) {
        const cells = parseCsvLine(lines[i]);
        const manufacturer = (cells[manufacturerIdx] ?? "").trim();
        const partNumber = (cells[partNumberIdx] ?? "").trim();
        if (!partNumber) {
            continue;
        }
        rows.push({ manufacturer, partNumber });
    }
    return rows;
}
async function mapDatasheetsFromBom(bomCsvPath, datasheetPaths, resourcesDir) {
    await fs.mkdir(resourcesDir, { recursive: true });
    const bomRows = dedupeBomRows(await parseBomPartRows(bomCsvPath));
    const sourceByStem = new Map();
    const normalizedStemToPath = new Map();
    for (const fullPath of datasheetPaths) {
        const stem = path.basename(fullPath, path.extname(fullPath));
        sourceByStem.set(stem.toLowerCase(), fullPath);
        normalizedStemToPath.set(normalizeToken(stem), fullPath);
    }
    const mappings = [];
    const unmatchedParts = [];
    for (const row of bomRows) {
        const rawKey = row.partNumber.toLowerCase();
        const normalizedKey = normalizeToken(row.partNumber);
        const exact = sourceByStem.get(rawKey) ?? normalizedStemToPath.get(normalizedKey);
        const fuzzy = exact
            ? exact
            : datasheetPaths.find((p) => {
                const stem = path.basename(p, path.extname(p));
                const normalizedStem = normalizeToken(stem);
                return normalizedStem.includes(normalizedKey) || normalizedKey.includes(normalizedStem);
            });
        const sourcePath = fuzzy ?? "";
        if (!sourcePath) {
            unmatchedParts.push(row);
            continue;
        }
        const mappedPath = await copyDatasheetToPart(sourcePath, row.partNumber, resourcesDir);
        mappings.push({
            manufacturer: row.manufacturer,
            partNumber: row.partNumber,
            sourcePath,
            mappedPath,
        });
    }
    const unique = new Map();
    for (const mapping of mappings) {
        unique.set(normalizeToken(mapping.partNumber), mapping);
    }
    return {
        mappings: [...unique.values()].sort((a, b) => a.partNumber.localeCompare(b.partNumber)),
        unmatchedParts,
    };
}
//# sourceMappingURL=bomDatasheetMapper.js.map
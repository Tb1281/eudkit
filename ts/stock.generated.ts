/**
 * Generated from `schema/stock-rows.json` by `ts/scripts/generate-stock.ts`.
 * Do not edit by hand. Regenerate with `npm run generate:stock` from `ts/`.
 *
 * Every non-special stock and eudkit-extension helper is data-driven; the
 * special rows (Deaths, SetDeaths, CreateUnitWithProperties, SetStringDword,
 * UnitProperty) stay hand-written in `hir.ts`.
 */

import type { HirAct, HirCond, Trigger } from "./trigger.ts";
import type { TriggerFieldRef } from "./view.ts";
import {
  _emitStockAct,
  _emitStockCond,
  type DbRef,
  type PlayerOrFieldTarget,
  type ValueOrPtrTarget,
} from "./hir.ts";
import type { StockRowSpec } from "./stock-spec.ts";

const rowCountdownTimer: StockRowSpec = {
  id: "CountdownTimer",
  kind: "condition",
  chkType: 1,
  flags: 0,
  params: [
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "time", min: 0, max: 0xffffffff } },
  ],
};

const rowCommand: StockRowSpec = {
  id: "Command",
  kind: "condition",
  chkType: 2,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowBring: StockRowSpec = {
  id: "Bring",
  kind: "condition",
  chkType: 3,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "locid", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowAccumulate: StockRowSpec = {
  id: "Accumulate",
  kind: "condition",
  chkType: 4,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
  ],
};

const rowKills: StockRowSpec = {
  id: "Kills",
  kind: "condition",
  chkType: 5,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowCommandMost: StockRowSpec = {
  id: "CommandMost",
  kind: "condition",
  chkType: 6,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowCommandMostAt: StockRowSpec = {
  id: "CommandMostAt",
  kind: "condition",
  chkType: 7,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "locid", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowMostKills: StockRowSpec = {
  id: "MostKills",
  kind: "condition",
  chkType: 8,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowHighestScore: StockRowSpec = {
  id: "HighestScore",
  kind: "condition",
  chkType: 9,
  flags: 0,
  params: [
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
  ],
};

const rowMostResources: StockRowSpec = {
  id: "MostResources",
  kind: "condition",
  chkType: 10,
  flags: 0,
  params: [
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
  ],
};

const rowSwitch: StockRowSpec = {
  id: "Switch",
  kind: "condition",
  chkType: 11,
  flags: 0,
  params: [
    { field: "restype", encoding: "u8", validator: { kind: "range", label: "switch id", min: 0, max: 255 } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "switch state", values: [2, 3] } },
  ],
};

const rowElapsedTime: StockRowSpec = {
  id: "ElapsedTime",
  kind: "condition",
  chkType: 12,
  flags: 0,
  params: [
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "time", min: 0, max: 0xffffffff } },
  ],
};

const rowOpponents: StockRowSpec = {
  id: "Opponents",
  kind: "condition",
  chkType: 14,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
  ],
};

const rowCommandLeast: StockRowSpec = {
  id: "CommandLeast",
  kind: "condition",
  chkType: 16,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowCommandLeastAt: StockRowSpec = {
  id: "CommandLeastAt",
  kind: "condition",
  chkType: 17,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "locid", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowLeastKills: StockRowSpec = {
  id: "LeastKills",
  kind: "condition",
  chkType: 18,
  flags: 0,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
  ],
};

const rowLowestScore: StockRowSpec = {
  id: "LowestScore",
  kind: "condition",
  chkType: 19,
  flags: 0,
  params: [
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
  ],
};

const rowLeastResources: StockRowSpec = {
  id: "LeastResources",
  kind: "condition",
  chkType: 20,
  flags: 0,
  params: [
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
  ],
};

const rowScore: StockRowSpec = {
  id: "Score",
  kind: "condition",
  chkType: 21,
  flags: 0,
  params: [
    { field: "player", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "restype", encoding: "u8", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
    { field: "comparison", encoding: "u8", validator: { kind: "enum", label: "comparison", values: [0, 1, 10] } },
    { field: "amount", encoding: "u32", validator: { kind: "range", label: "number", min: 0, max: 0xffffffff } },
  ],
};

const rowAlways: StockRowSpec = {
  id: "Always",
  kind: "condition",
  chkType: 22,
  flags: 0,
  params: [

  ],
};

const rowNever: StockRowSpec = {
  id: "Never",
  kind: "condition",
  chkType: 23,
  flags: 0,
  params: [

  ],
};

const rowVictory: StockRowSpec = {
  id: "Victory",
  kind: "action",
  chkType: 1,
  flags: 4,
  params: [

  ],
};

const rowDefeat: StockRowSpec = {
  id: "Defeat",
  kind: "action",
  chkType: 2,
  flags: 4,
  params: [

  ],
};

const rowPreserveTrigger: StockRowSpec = {
  id: "PreserveTrigger",
  kind: "action",
  chkType: 3,
  flags: 4,
  params: [

  ],
};

const rowWait: StockRowSpec = {
  id: "Wait",
  kind: "action",
  chkType: 4,
  flags: 4,
  params: [
    { field: "time", encoding: "u32-wrap", validator: { kind: "none" } },
  ],
};

const rowPauseGame: StockRowSpec = {
  id: "PauseGame",
  kind: "action",
  chkType: 5,
  flags: 4,
  params: [

  ],
};

const rowUnpauseGame: StockRowSpec = {
  id: "UnpauseGame",
  kind: "action",
  chkType: 6,
  flags: 4,
  params: [

  ],
};

const rowTransmission: StockRowSpec = {
  id: "Transmission",
  kind: "action",
  chkType: 7,
  flags: 4,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "wav", encoding: "string", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "modifier", values: [7, 8, 9] } },
    { field: "time", encoding: "u32", validator: { kind: "range", label: "time", min: 0, max: 0xffffffff } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
    { field: "flags", encoding: "u8", validator: { kind: "range", label: "always display", min: 0, max: 255 } },
  ],
};

const rowPlayWAV: StockRowSpec = {
  id: "PlayWAV",
  kind: "action",
  chkType: 8,
  flags: 4,
  params: [
    { field: "wav", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowDisplayText: StockRowSpec = {
  id: "DisplayText",
  kind: "action",
  chkType: 9,
  flags: 4,
  params: [
    { field: "text", encoding: "string", validator: { kind: "none" } },
    { field: "flags", encoding: "u8-truncate", validator: { kind: "none" } },
  ],
};

const rowCenterView: StockRowSpec = {
  id: "CenterView",
  kind: "action",
  chkType: 10,
  flags: 4,
  params: [
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowSetMissionObjectives: StockRowSpec = {
  id: "SetMissionObjectives",
  kind: "action",
  chkType: 12,
  flags: 4,
  params: [
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowSetSwitch: StockRowSpec = {
  id: "SetSwitch",
  kind: "action",
  chkType: 13,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "switch id", min: 0, max: 255 } },
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "switch action", values: [4, 5, 6, 11] } },
  ],
};

const rowSetCountdownTimer: StockRowSpec = {
  id: "SetCountdownTimer",
  kind: "action",
  chkType: 14,
  flags: 4,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "modifier", values: [7, 8, 9] } },
    { field: "time", encoding: "u32", validator: { kind: "range", label: "time", min: 0, max: 0xffffffff } },
  ],
};

const rowRunAIScript: StockRowSpec = {
  id: "RunAIScript",
  kind: "action",
  chkType: 15,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "AI script", min: 0, max: 0xffffffff } },
  ],
};

const rowRunAIScriptAt: StockRowSpec = {
  id: "RunAIScriptAt",
  kind: "action",
  chkType: 16,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "AI script", min: 0, max: 0xffffffff } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowLeaderBoardControl: StockRowSpec = {
  id: "LeaderBoardControl",
  kind: "action",
  chkType: 17,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardControlAt: StockRowSpec = {
  id: "LeaderBoardControlAt",
  kind: "action",
  chkType: 18,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardResources: StockRowSpec = {
  id: "LeaderBoardResources",
  kind: "action",
  chkType: 19,
  flags: 4,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardKills: StockRowSpec = {
  id: "LeaderBoardKills",
  kind: "action",
  chkType: 20,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardScore: StockRowSpec = {
  id: "LeaderBoardScore",
  kind: "action",
  chkType: 21,
  flags: 4,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowKillUnit: StockRowSpec = {
  id: "KillUnit",
  kind: "action",
  chkType: 22,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowKillUnitAt: StockRowSpec = {
  id: "KillUnitAt",
  kind: "action",
  chkType: 23,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowRemoveUnit: StockRowSpec = {
  id: "RemoveUnit",
  kind: "action",
  chkType: 24,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowRemoveUnitAt: StockRowSpec = {
  id: "RemoveUnitAt",
  kind: "action",
  chkType: 25,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowSetResources: StockRowSpec = {
  id: "SetResources",
  kind: "action",
  chkType: 26,
  flags: 4,
  params: [
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "modifier", values: [7, 8, 9] } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "amount", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
  ],
};

const rowSetScore: StockRowSpec = {
  id: "SetScore",
  kind: "action",
  chkType: 27,
  flags: 4,
  params: [
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "modifier", values: [7, 8, 9] } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "amount", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
  ],
};

const rowMinimapPing: StockRowSpec = {
  id: "MinimapPing",
  kind: "action",
  chkType: 28,
  flags: 4,
  params: [
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowTalkingPortrait: StockRowSpec = {
  id: "TalkingPortrait",
  kind: "action",
  chkType: 29,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "time", encoding: "u32", validator: { kind: "range", label: "time", min: 0, max: 0xffffffff } },
  ],
};

const rowMuteUnitSpeech: StockRowSpec = {
  id: "MuteUnitSpeech",
  kind: "action",
  chkType: 30,
  flags: 4,
  params: [

  ],
};

const rowUnMuteUnitSpeech: StockRowSpec = {
  id: "UnMuteUnitSpeech",
  kind: "action",
  chkType: 31,
  flags: 4,
  params: [

  ],
};

const rowLeaderBoardComputerPlayers: StockRowSpec = {
  id: "LeaderBoardComputerPlayers",
  kind: "action",
  chkType: 32,
  flags: 4,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "property state", values: [4, 5, 6] } },
  ],
};

const rowLeaderBoardGoalControl: StockRowSpec = {
  id: "LeaderBoardGoalControl",
  kind: "action",
  chkType: 33,
  flags: 20,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardGoalControlAt: StockRowSpec = {
  id: "LeaderBoardGoalControlAt",
  kind: "action",
  chkType: 34,
  flags: 20,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardGoalResources: StockRowSpec = {
  id: "LeaderBoardGoalResources",
  kind: "action",
  chkType: 35,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "resource type", values: [0, 1, 2] } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardGoalKills: StockRowSpec = {
  id: "LeaderBoardGoalKills",
  kind: "action",
  chkType: 36,
  flags: 20,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowLeaderBoardGoalScore: StockRowSpec = {
  id: "LeaderBoardGoalScore",
  kind: "action",
  chkType: 37,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "score type", values: [0, 1, 2, 3, 4, 5, 6, 7] } },
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowMoveLocation: StockRowSpec = {
  id: "MoveLocation",
  kind: "action",
  chkType: 38,
  flags: 20,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowMoveUnit: StockRowSpec = {
  id: "MoveUnit",
  kind: "action",
  chkType: 39,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowLeaderBoardGreed: StockRowSpec = {
  id: "LeaderBoardGreed",
  kind: "action",
  chkType: 40,
  flags: 4,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "goal", min: 0, max: 0xffffffff } },
  ],
};

const rowSetNextScenario: StockRowSpec = {
  id: "SetNextScenario",
  kind: "action",
  chkType: 41,
  flags: 4,
  params: [
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowSetDoodadState: StockRowSpec = {
  id: "SetDoodadState",
  kind: "action",
  chkType: 42,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "property state", values: [4, 5, 6] } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowSetInvincibility: StockRowSpec = {
  id: "SetInvincibility",
  kind: "action",
  chkType: 43,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "property state", values: [4, 5, 6] } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowCreateUnit: StockRowSpec = {
  id: "CreateUnit",
  kind: "action",
  chkType: 44,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "range", label: "count", min: 1, max: 255 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowOrder: StockRowSpec = {
  id: "Order",
  kind: "action",
  chkType: 46,
  flags: 20,
  params: [
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "modifier", encoding: "u8", validator: { kind: "enum", label: "order", values: [0, 1, 2] } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowComment: StockRowSpec = {
  id: "Comment",
  kind: "action",
  chkType: 47,
  flags: 4,
  params: [
    { field: "text", encoding: "string", validator: { kind: "none" } },
  ],
};

const rowGiveUnits: StockRowSpec = {
  id: "GiveUnits",
  kind: "action",
  chkType: 48,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
  ],
};

const rowModifyUnitHitPoints: StockRowSpec = {
  id: "ModifyUnitHitPoints",
  kind: "action",
  chkType: 49,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "percent", min: 0, max: 100 } },
  ],
};

const rowModifyUnitEnergy: StockRowSpec = {
  id: "ModifyUnitEnergy",
  kind: "action",
  chkType: 50,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "percent", min: 0, max: 100 } },
  ],
};

const rowModifyUnitShields: StockRowSpec = {
  id: "ModifyUnitShields",
  kind: "action",
  chkType: 51,
  flags: 20,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "percent", min: 0, max: 100 } },
  ],
};

const rowModifyUnitResourceAmount: StockRowSpec = {
  id: "ModifyUnitResourceAmount",
  kind: "action",
  chkType: 52,
  flags: 4,
  params: [
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
    { field: "value", encoding: "u32", validator: { kind: "range", label: "new value", min: 0, max: 0xffffffff } },
  ],
};

const rowModifyUnitHangarCount: StockRowSpec = {
  id: "ModifyUnitHangarCount",
  kind: "action",
  chkType: 53,
  flags: 20,
  params: [
    { field: "value", encoding: "u32", validator: { kind: "range", label: "add", min: 0, max: 0xffffffff } },
    { field: "modifier", encoding: "u8", validator: { kind: "count", label: "count", min: 1, max: 255, all: 0 } },
    { field: "unit", encoding: "u16", validator: { kind: "range", label: "unit", min: 0, max: 227 } },
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "mask", encoding: "u32", validator: { kind: "range", label: "location", min: 0, max: 255 } },
  ],
};

const rowPauseTimer: StockRowSpec = {
  id: "PauseTimer",
  kind: "action",
  chkType: 54,
  flags: 4,
  params: [

  ],
};

const rowUnpauseTimer: StockRowSpec = {
  id: "UnpauseTimer",
  kind: "action",
  chkType: 55,
  flags: 4,
  params: [

  ],
};

const rowDraw: StockRowSpec = {
  id: "Draw",
  kind: "action",
  chkType: 56,
  flags: 4,
  params: [

  ],
};

const rowSetAllianceStatus: StockRowSpec = {
  id: "SetAllianceStatus",
  kind: "action",
  chkType: 57,
  flags: 4,
  params: [
    { field: "dest", encoding: "u32", validator: { kind: "enum", label: "player", values: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 26] } },
    { field: "unit", encoding: "u16", validator: { kind: "enum", label: "alliance status", values: [0, 1, 2] } },
  ],
};

const rowDeathsX: StockRowSpec = {
  id: "DeathsX",
  kind: "condition",
  chkType: 15,
  flags: 0,
  eudx: 0x4353,
  params: [
    { field: "player", encoding: "epd", validator: { kind: "none" } },
    { field: "comparison", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "amount", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "unit", encoding: "u16-truncate", validator: { kind: "none" } },
    { field: "locid", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowMemory: StockRowSpec = {
  id: "Memory",
  kind: "condition",
  chkType: 15,
  flags: 0,
  params: [
    { field: "player", encoding: "epd-from-address", validator: { kind: "none" } },
    { field: "comparison", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "amount", encoding: "value-or-ptr", validator: { kind: "none" } },
  ],
};

const rowMemoryEPD: StockRowSpec = {
  id: "MemoryEPD",
  kind: "condition",
  chkType: 15,
  flags: 0,
  params: [
    { field: "player", encoding: "epd", validator: { kind: "none" } },
    { field: "comparison", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "amount", encoding: "value-or-ptr", validator: { kind: "none" } },
  ],
};

const rowMemoryX: StockRowSpec = {
  id: "MemoryX",
  kind: "condition",
  chkType: 15,
  flags: 0,
  eudx: 0x4353,
  params: [
    { field: "player", encoding: "epd-from-address", validator: { kind: "none" } },
    { field: "comparison", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "amount", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "locid", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowMemoryXEPD: StockRowSpec = {
  id: "MemoryXEPD",
  kind: "condition",
  chkType: 15,
  flags: 0,
  eudx: 0x4353,
  params: [
    { field: "player", encoding: "epd", validator: { kind: "none" } },
    { field: "comparison", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "amount", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "locid", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowSetDeathsX: StockRowSpec = {
  id: "SetDeathsX",
  kind: "action",
  chkType: 45,
  flags: 20,
  eudx: 0x4353,
  params: [
    { field: "dest", encoding: "epd", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "unit", encoding: "u16-truncate", validator: { kind: "none" } },
    { field: "mask", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowSetMemory: StockRowSpec = {
  id: "SetMemory",
  kind: "action",
  chkType: 45,
  flags: 20,
  params: [
    { field: "dest", encoding: "epd-from-address", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
  ],
};

const rowSetMemoryEPD: StockRowSpec = {
  id: "SetMemoryEPD",
  kind: "action",
  chkType: 45,
  flags: 20,
  params: [
    { field: "dest", encoding: "epd", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
  ],
};

const rowSetMemoryX: StockRowSpec = {
  id: "SetMemoryX",
  kind: "action",
  chkType: 45,
  flags: 20,
  eudx: 0x4353,
  params: [
    { field: "dest", encoding: "epd-from-address", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "mask", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowSetMemoryXEPD: StockRowSpec = {
  id: "SetMemoryXEPD",
  kind: "action",
  chkType: 45,
  flags: 20,
  eudx: 0x4353,
  params: [
    { field: "dest", encoding: "epd", validator: { kind: "none" } },
    { field: "modifier", encoding: "u8-truncate", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
    { field: "mask", encoding: "mask", validator: { kind: "none" } },
  ],
};

const rowSetNextPtr: StockRowSpec = {
  id: "SetNextPtr",
  kind: "action",
  chkType: 45,
  flags: 20,
  modifier: 7,
  params: [
    { field: "relocs", encoding: "ptr", validator: { kind: "none" } },
    { field: "value", encoding: "value-or-ptr", validator: { kind: "none" } },
  ],
};

const rowDisplayTextIndex: StockRowSpec = {
  id: "DisplayTextIndex",
  kind: "action",
  chkType: 9,
  flags: 4,
  params: [
    { field: "str_index", encoding: "string-index", validator: { kind: "range", label: "string index", min: 0 } },
    { field: "flags", encoding: "u8-truncate", validator: { kind: "none" } },
  ],
};

/** Countdown timer is `comparison` `time` game seconds (condition 1). */
export function CountdownTimer(comparison: number, time: number): HirCond {
  return _emitStockCond(rowCountdownTimer, [comparison, time]);
}

/** `player` commands `number` units of `unit` (condition 2). */
export function Command(player: number, comparison: number, number: number, unit: number): HirCond {
  return _emitStockCond(rowCommand, [player, comparison, number, unit]);
}

/** Player brings `number` units of `unit` to `location` (condition 3). */
export function Bring(player: number, comparison: number, number: number, unit: number, location: number): HirCond {
  return _emitStockCond(rowBring, [player, comparison, number, unit, location]);
}

/** Player accumulates `number` of `resource_type` (condition 4). */
export function Accumulate(player: number, comparison: number, number: number, resourceType: number): HirCond {
  return _emitStockCond(rowAccumulate, [player, comparison, number, resourceType]);
}

/** `player` has `comparison` `number` kills of `unit` (condition 5). */
export function Kills(player: number, comparison: number, number: number, unit: number): HirCond {
  return _emitStockCond(rowKills, [player, comparison, number, unit]);
}

/** Current player commands the most `unit` (condition 6). */
export function CommandMost(unit: number): HirCond {
  return _emitStockCond(rowCommandMost, [unit]);
}

/** Current player commands the most `unit` at `location` (condition 7). */
export function CommandMostAt(unit: number, location: number): HirCond {
  return _emitStockCond(rowCommandMostAt, [unit, location]);
}

/** Current player has the most kills of `unit` (condition 8). */
export function MostKills(unit: number): HirCond {
  return _emitStockCond(rowMostKills, [unit]);
}

/** Current player has the highest `score_type` score (condition 9). */
export function HighestScore(scoreType: number): HirCond {
  return _emitStockCond(rowHighestScore, [scoreType]);
}

/** Current player has the most `resource_type` (condition 10). */
export function MostResources(resourceType: number): HirCond {
  return _emitStockCond(rowMostResources, [resourceType]);
}

/** `switch_id` is set or cleared (condition 11). */
export function Switch(switchId: number, state: number): HirCond {
  return _emitStockCond(rowSwitch, [switchId, state]);
}

/** Elapsed scenario time is `comparison` `time` game seconds (condition 12). */
export function ElapsedTime(comparison: number, time: number): HirCond {
  return _emitStockCond(rowElapsedTime, [comparison, time]);
}

/** `player` has `comparison` `number` opponents remaining (condition 14). */
export function Opponents(player: number, comparison: number, number: number): HirCond {
  return _emitStockCond(rowOpponents, [player, comparison, number]);
}

/** Current player commands the least `unit` (condition 16). */
export function CommandLeast(unit: number): HirCond {
  return _emitStockCond(rowCommandLeast, [unit]);
}

/** Current player commands the least `unit` at `location` (condition 17). */
export function CommandLeastAt(unit: number, location: number): HirCond {
  return _emitStockCond(rowCommandLeastAt, [unit, location]);
}

/** Current player has the least kills of `unit` (condition 18). */
export function LeastKills(unit: number): HirCond {
  return _emitStockCond(rowLeastKills, [unit]);
}

/** Current player has the lowest `score_type` score (condition 19). */
export function LowestScore(scoreType: number): HirCond {
  return _emitStockCond(rowLowestScore, [scoreType]);
}

/** Current player has the least `resource_type` (condition 20). */
export function LeastResources(resourceType: number): HirCond {
  return _emitStockCond(rowLeastResources, [resourceType]);
}

/** `player`'s `score_type` score is `comparison` `number` (condition 21). */
export function Score(player: number, scoreType: number, comparison: number, number: number): HirCond {
  return _emitStockCond(rowScore, [player, scoreType, comparison, number]);
}

/** Always true; the row takes no parameters (condition 22). */
export function Always(): HirCond {
  return _emitStockCond(rowAlways, []);
}

/** Never true; the row takes no parameters (condition 23). */
export function Never(): HirCond {
  return _emitStockCond(rowNever, []);
}

/** End the scenario in victory for the affected players (action 1). */
export function Victory(): HirAct {
  return _emitStockAct(rowVictory, []);
}

/** End the scenario in defeat for the affected players (action 2). */
export function Defeat(): HirAct {
  return _emitStockAct(rowDefeat, []);
}

/** Keep the trigger enabled after it fires (action 3). */
export function PreserveTrigger(): HirAct {
  return _emitStockAct(rowPreserveTrigger, []);
}

/** Wait `time` milliseconds before running the trigger's remaining actions (action 4). */
export function Wait(time: number): HirAct {
  return _emitStockAct(rowWait, [time]);
}

/** Pause the game (action 5). */
export function PauseGame(): HirAct {
  return _emitStockAct(rowPauseGame, []);
}

/** Unpause the game (action 6). */
export function UnpauseGame(): HirAct {
  return _emitStockAct(rowUnpauseGame, []);
}

/** Show `unit` at `location` for `time` milliseconds, play `sound`, and display `text` (action 7). `sound` resolves to `wavid`, `text` to `strid`. */
export function Transmission(unit: number, location: number, sound: string, timeModifier: number, time: number, text: string, alwaysDisplay: number = 4): HirAct {
  return _emitStockAct(rowTransmission, [unit, location, sound, timeModifier, time, text, alwaysDisplay]);
}

/** Play `wav` (action 8). The name resolves to the action's `wavid` slot. */
export function PlayWAV(wav: string): HirAct {
  return _emitStockAct(rowPlayWAV, [wav]);
}

/** Show `text` to CurrentPlayer (`0x6509B0`), not to all players (action 9). `always_display` defaults to 4. */
export function DisplayText(text: string, alwaysDisplay: number = 4): HirAct {
  return _emitStockAct(rowDisplayText, [text, alwaysDisplay]);
}

/** Center the current player's view on `location` (action 10). */
export function CenterView(location: number): HirAct {
  return _emitStockAct(rowCenterView, [location]);
}

/** Replace the mission objectives text (action 12). */
export function SetMissionObjectives(text: string): HirAct {
  return _emitStockAct(rowSetMissionObjectives, [text]);
}

/** Set, clear, toggle, or randomize `switchId` (action 13). */
export function SetSwitch(switchId: number, state: number): HirAct {
  return _emitStockAct(rowSetSwitch, [switchId, state]);
}

/** Set or adjust the countdown timer (action 14). */
export function SetCountdownTimer(modifier: number, time: number): HirAct {
  return _emitStockAct(rowSetCountdownTimer, [modifier, time]);
}

/** Run AI script `script` for the trigger owner (action 15). */
export function RunAIScript(script: number): HirAct {
  return _emitStockAct(rowRunAIScript, [script]);
}

/** Run AI script `script` at `location` (action 16). */
export function RunAIScriptAt(script: number, location: number): HirAct {
  return _emitStockAct(rowRunAIScriptAt, [script, location]);
}

/** Show a leaderboard for control of `unit` (action 17). */
export function LeaderBoardControl(unit: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardControl, [unit, label]);
}

/** Show a leaderboard for control of `unit` at `location` (action 18). */
export function LeaderBoardControlAt(unit: number, location: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardControlAt, [unit, location, label]);
}

/** Show a leaderboard for accumulated `resourceType` (action 19). */
export function LeaderBoardResources(resourceType: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardResources, [resourceType, label]);
}

/** Show a leaderboard for kills of `unit` (action 20). */
export function LeaderBoardKills(unit: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardKills, [unit, label]);
}

/** Show a leaderboard for `scoreType` (action 21). */
export function LeaderBoardScore(scoreType: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardScore, [scoreType, label]);
}

/** Kill every `unit` owned by `player` (action 22). */
export function KillUnit(unit: number, player: number): HirAct {
  return _emitStockAct(rowKillUnit, [unit, player]);
}

/** Kill `count` units of `unit` owned by `player` at `location` (action 23). */
export function KillUnitAt(count: number, unit: number, location: number, player: number): HirAct {
  return _emitStockAct(rowKillUnitAt, [count, unit, location, player]);
}

/** Remove every `unit` owned by `player` (action 24). */
export function RemoveUnit(unit: number, player: number): HirAct {
  return _emitStockAct(rowRemoveUnit, [unit, player]);
}

/** Remove `count` units of `unit` owned by `player` at `location` (action 25). */
export function RemoveUnitAt(count: number, unit: number, location: number, player: number): HirAct {
  return _emitStockAct(rowRemoveUnitAt, [count, unit, location, player]);
}

/** Set, add, or subtract player resources (action 26). */
export function SetResources(player: number, modifier: number, amount: number, resourceType: number): HirAct {
  return _emitStockAct(rowSetResources, [player, modifier, amount, resourceType]);
}

/** Set, add, or subtract a player score (action 27). */
export function SetScore(player: number, modifier: number, amount: number, scoreType: number): HirAct {
  return _emitStockAct(rowSetScore, [player, modifier, amount, scoreType]);
}

/** Ping the current player's minimap at `location` (action 28). */
export function MinimapPing(location: number): HirAct {
  return _emitStockAct(rowMinimapPing, [location]);
}

/** Show `unit`'s portrait for `time` milliseconds (action 29). */
export function TalkingPortrait(unit: number, time: number): HirAct {
  return _emitStockAct(rowTalkingPortrait, [unit, time]);
}

/** Mute non-trigger unit sounds (action 30). */
export function MuteUnitSpeech(): HirAct {
  return _emitStockAct(rowMuteUnitSpeech, []);
}

/** Unmute non-trigger unit sounds (action 31). */
export function UnMuteUnitSpeech(): HirAct {
  return _emitStockAct(rowUnMuteUnitSpeech, []);
}

/** Include or exclude computer players in leaderboards (action 32). */
export function LeaderBoardComputerPlayers(state: number): HirAct {
  return _emitStockAct(rowLeaderBoardComputerPlayers, [state]);
}

/** Show a leaderboard for progress toward controlling `goal` units (action 33). */
export function LeaderBoardGoalControl(goal: number, unit: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardGoalControl, [goal, unit, label]);
}

/** Show a leaderboard for progress toward controlling `goal` units at `location` (action 34). */
export function LeaderBoardGoalControlAt(goal: number, unit: number, location: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardGoalControlAt, [goal, unit, location, label]);
}

/** Show a leaderboard for progress toward `goal` resources (action 35). */
export function LeaderBoardGoalResources(goal: number, resourceType: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardGoalResources, [goal, resourceType, label]);
}

/** Show a leaderboard for progress toward `goal` kills (action 36). */
export function LeaderBoardGoalKills(goal: number, unit: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardGoalKills, [goal, unit, label]);
}

/** Show a leaderboard for progress toward `goal` score (action 37). */
export function LeaderBoardGoalScore(goal: number, scoreType: number, label: string): HirAct {
  return _emitStockAct(rowLeaderBoardGoalScore, [goal, scoreType, label]);
}

/** Center `location` on `onUnit` owned by `owner` inside `searchLocation` (action 38). The fourth parameter is the search location. */
export function MoveLocation(location: number, onUnit: number, owner: number, searchLocation: number): HirAct {
  return _emitStockAct(rowMoveLocation, [location, onUnit, owner, searchLocation]);
}

/** Move `count` units of `unit` from `startLocation` to `destLocation` (action 39). */
export function MoveUnit(count: number, unit: number, owner: number, startLocation: number, destLocation: number): HirAct {
  return _emitStockAct(rowMoveUnit, [count, unit, owner, startLocation, destLocation]);
}

/** Show the greed leaderboard for accumulated ore and gas (action 40). */
export function LeaderBoardGreed(goal: number): HirAct {
  return _emitStockAct(rowLeaderBoardGreed, [goal]);
}

/** Set the next campaign scenario name (action 41). */
export function SetNextScenario(scenarioName: string): HirAct {
  return _emitStockAct(rowSetNextScenario, [scenarioName]);
}

/** Set doodad `state` for `unit` owned by `owner` at `location` (action 42). */
export function SetDoodadState(state: number, unit: number, owner: number, location: number): HirAct {
  return _emitStockAct(rowSetDoodadState, [state, unit, owner, location]);
}

/** Set invincibility `state` for `unit` owned by `owner` at `location` (action 43). */
export function SetInvincibility(state: number, unit: number, owner: number, location: number): HirAct {
  return _emitStockAct(rowSetInvincibility, [state, unit, owner, location]);
}

/** Create `number` units of `unit` at `location` for `player` (action 44). */
export function CreateUnit(number: number, unit: number, location: number, player: number): HirAct {
  return _emitStockAct(rowCreateUnit, [number, unit, location, player]);
}

/** Order `unit` owned by `owner` at `startLocation` to `destLocation` with `orderType` (action 46). */
export function Order(unit: number, owner: number, startLocation: number, orderType: number, destLocation: number): HirAct {
  return _emitStockAct(rowOrder, [unit, owner, startLocation, orderType, destLocation]);
}

/** Attach a comment string to the trigger (action 47). */
export function Comment(text: string): HirAct {
  return _emitStockAct(rowComment, [text]);
}

/** Give `count` units of `unit` at `location` to `newOwner` (action 48). */
export function GiveUnits(count: number, unit: number, owner: number, location: number, newOwner: number): HirAct {
  return _emitStockAct(rowGiveUnits, [count, unit, owner, location, newOwner]);
}

/** Set hit points to `percent` for `count` units at `location` (action 49). */
export function ModifyUnitHitPoints(count: number, unit: number, owner: number, location: number, percent: number): HirAct {
  return _emitStockAct(rowModifyUnitHitPoints, [count, unit, owner, location, percent]);
}

/** Set energy to `percent` for `count` units at `location` (action 50). */
export function ModifyUnitEnergy(count: number, unit: number, owner: number, location: number, percent: number): HirAct {
  return _emitStockAct(rowModifyUnitEnergy, [count, unit, owner, location, percent]);
}

/** Set shields to `percent` for `count` units at `location` (action 51). */
export function ModifyUnitShields(count: number, unit: number, owner: number, location: number, percent: number): HirAct {
  return _emitStockAct(rowModifyUnitShields, [count, unit, owner, location, percent]);
}

/** Set the resource amount of `count` resource sources at `location` to `newValue` (action 52). */
export function ModifyUnitResourceAmount(count: number, owner: number, location: number, newValue: number): HirAct {
  return _emitStockAct(rowModifyUnitResourceAmount, [count, owner, location, newValue]);
}

/** Add `add` to the hangar count of `count` units at `location` (action 53). */
export function ModifyUnitHangarCount(add: number, count: number, unit: number, owner: number, location: number): HirAct {
  return _emitStockAct(rowModifyUnitHangarCount, [add, count, unit, owner, location]);
}

/** Pause the countdown timer (action 54). */
export function PauseTimer(): HirAct {
  return _emitStockAct(rowPauseTimer, []);
}

/** Resume the countdown timer (action 55). */
export function UnpauseTimer(): HirAct {
  return _emitStockAct(rowUnpauseTimer, []);
}

/** End the scenario in a draw for the affected players (action 56). */
export function Draw(): HirAct {
  return _emitStockAct(rowDraw, []);
}

/** Set `player`'s alliance status (action 57). */
export function SetAllianceStatus(player: number, status: number): HirAct {
  return _emitStockAct(rowSetAllianceStatus, [player, status]);
}

/** Masked Deaths-table read (condition 15 with the `eudx` mask). Eudkit extension; not a StarEdit row. */
export function DeathsX(player: PlayerOrFieldTarget, comparison: number, amount: ValueOrPtrTarget, unit: number = 0, mask: number = 0xffffffff): HirCond {
  return _emitStockCond(rowDeathsX, [player, comparison, amount, unit, mask]);
}

/** Read memory at a fixed address through the Deaths wire form (condition 15 at `EPD(addr)`). Eudkit extension. */
export function Memory(addr: number | DbRef, comparison: number, amount: ValueOrPtrTarget): HirCond {
  return _emitStockCond(rowMemory, [addr, comparison, amount]);
}

/** Read memory at an EPD through the Deaths wire form (condition 15). Eudkit extension. */
export function MemoryEPD(epdAddr: PlayerOrFieldTarget, comparison: number, amount: ValueOrPtrTarget): HirCond {
  return _emitStockCond(rowMemoryEPD, [epdAddr, comparison, amount]);
}

/** Masked read of memory at a fixed address (condition 15 with the `eudx` mask). Eudkit extension. */
export function MemoryX(addr: number | DbRef, comparison: number, amount: ValueOrPtrTarget, mask: number): HirCond {
  return _emitStockCond(rowMemoryX, [addr, comparison, amount, mask]);
}

/** Masked read of memory at an EPD (condition 15 with the `eudx` mask). Eudkit extension. */
export function MemoryXEPD(epdAddr: PlayerOrFieldTarget, comparison: number, amount: ValueOrPtrTarget, mask: number): HirCond {
  return _emitStockCond(rowMemoryXEPD, [epdAddr, comparison, amount, mask]);
}

/** Masked Deaths-table write (action 45 with the `eudx` mask). Eudkit extension; not a StarEdit row. */
export function SetDeathsX(player: PlayerOrFieldTarget, modifier: number, value: ValueOrPtrTarget, unit: number = 0, mask: number = 0xffffffff): HirAct {
  return _emitStockAct(rowSetDeathsX, [player, modifier, value, unit, mask]);
}

/** Write memory at a fixed address through the Deaths wire form (action 45 at `EPD(addr)`). Eudkit extension. */
export function SetMemory(addr: number | DbRef, modifier: number, value: ValueOrPtrTarget): HirAct {
  return _emitStockAct(rowSetMemory, [addr, modifier, value]);
}

/** Write memory at an EPD through the Deaths wire form (action 45). Eudkit extension. */
export function SetMemoryEPD(epdAddr: PlayerOrFieldTarget, modifier: number, value: ValueOrPtrTarget): HirAct {
  return _emitStockAct(rowSetMemoryEPD, [epdAddr, modifier, value]);
}

/** Masked write of memory at a fixed address (action 45 with the `eudx` mask). Eudkit extension. */
export function SetMemoryX(addr: number | DbRef, modifier: number, value: ValueOrPtrTarget, mask: number): HirAct {
  return _emitStockAct(rowSetMemoryX, [addr, modifier, value, mask]);
}

/** Masked write of memory at an EPD (action 45 with the `eudx` mask). Eudkit extension. */
export function SetMemoryXEPD(epdAddr: PlayerOrFieldTarget, modifier: number, value: ValueOrPtrTarget, mask: number): HirAct {
  return _emitStockAct(rowSetMemoryXEPD, [epdAddr, modifier, value, mask]);
}

/** Write a trigger `next` pointer through the Deaths wire form (action 45 relocation). Eudkit extension. */
export function SetNextPtr(src: Trigger | TriggerFieldRef, dst: ValueOrPtrTarget): HirAct {
  return _emitStockAct(rowSetNextPtr, [src, dst]);
}

/** Show the module string at `index` (0-based module string index) to CurrentPlayer (action 9). Eudkit extension. */
export function DisplayTextIndex(index: number, alwaysDisplay: number = 4): HirAct {
  return _emitStockAct(rowDisplayTextIndex, [index, alwaysDisplay]);
}

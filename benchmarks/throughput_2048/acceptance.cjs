#!/usr/bin/env node

"use strict";

// Frozen, UI-free acceptance checks for the 2048 game-logic experiment.
// Usage: node acceptance.cjs REPO_DIR GROUP
// GROUP is baseline, undo, moves, analysis, storage, all, or a comma-separated
// combination of those names.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const GROUPS = ["baseline", "undo", "moves", "analysis", "storage"];

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function plain(value) {
  return clone(value);
}

function loadGame(repoDir) {
  const context = vm.createContext({
    console,
    Math,
    window: {}
  });

  for (const file of ["tile.js", "grid.js", "game_manager.js", "local_storage_manager.js"]) {
    const filename = path.join(repoDir, "js", file);
    if (!fs.existsSync(filename)) {
      throw new Error(`missing upstream game file: ${filename}`);
    }
    vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
  }

  return context;
}

function cellsFor(size, placements) {
  const cells = [];
  for (let x = 0; x < size; x += 1) {
    cells[x] = [];
    for (let y = 0; y < size; y += 1) {
      cells[x][y] = null;
    }
  }
  for (const [x, y, value] of placements) {
    cells[x][y] = { position: { x, y }, value };
  }
  return cells;
}

function gameState(size, placements, options = {}) {
  return {
    grid: { size, cells: cellsFor(size, placements) },
    score: options.score === undefined ? 0 : options.score,
    over: options.over === undefined ? false : options.over,
    won: options.won === undefined ? false : options.won,
    keepPlaying: options.keepPlaying === undefined ? false : options.keepPlaying,
    ...(options.moveCount === undefined ? {} : { moveCount: options.moveCount })
  };
}

function installBoard(context, manager, size, placements) {
  const grid = new context.Grid(size);
  for (const [x, y, value] of placements) {
    grid.insertTile(new context.Tile({ x, y }, value));
  }
  manager.grid = grid;
}

function nonEmptyCount(grid) {
  let count = 0;
  grid.eachCell((x, y, tile) => {
    if (tile) count += 1;
  });
  return count;
}

function lastMetadata(actuator) {
  assert.ok(actuator.calls.length > 0, "expected an actuator update");
  return actuator.calls[actuator.calls.length - 1].metadata;
}

function makeHarness(context, size, savedState, bestScore = 0) {
  const memory = {
    gameState: savedState === undefined ? null : JSON.stringify(savedState),
    bestScore
  };
  let actuator;

  function InputManager() {
    this.listeners = Object.create(null);
  }

  InputManager.prototype.on = function (event, callback) {
    (this.listeners[event] || (this.listeners[event] = [])).push(callback);
  };

  InputManager.prototype.emit = function (event, value) {
    for (const callback of this.listeners[event] || []) callback(value);
  };

  function Actuator() {
    this.calls = [];
    this.continueCalls = 0;
    actuator = this;
  }

  Actuator.prototype.actuate = function (grid, metadata) {
    this.calls.push({ grid: plain(grid.serialize()), metadata: plain(metadata) });
  };

  Actuator.prototype.continueGame = function () {
    this.continueCalls += 1;
  };

  function StorageManager() {}

  StorageManager.prototype.getGameState = function () {
    return memory.gameState === null ? null : JSON.parse(memory.gameState);
  };

  StorageManager.prototype.setGameState = function (state) {
    memory.gameState = JSON.stringify(state);
  };

  StorageManager.prototype.clearGameState = function () {
    memory.gameState = null;
  };

  StorageManager.prototype.getBestScore = function () {
    return memory.bestScore || 0;
  };

  StorageManager.prototype.setBestScore = function (score) {
    memory.bestScore = score;
  };

  const manager = new context.GameManager(size, InputManager, Actuator, StorageManager);
  return { manager, actuator, input: manager.inputManager, memory };
}

function resetManager(manager, score = 0, flags = {}) {
  manager.score = score;
  manager.over = flags.over === undefined ? false : flags.over;
  manager.won = flags.won === undefined ? false : flags.won;
  manager.keepPlaying = flags.keepPlaying === undefined ? false : flags.keepPlaying;
}

function testBaselineMerge(context) {
  const harness = makeHarness(context, 4);
  const { manager, actuator, memory } = harness;
  actuator.calls.length = 0;
  installBoard(context, manager, 4, [[1, 0, 2], [2, 0, 2]]);
  resetManager(manager);

  manager.move(3); // left: one deterministic merge and one random spawn

  const merged = manager.grid.cellContent({ x: 0, y: 0 });
  assert.equal(merged.value, 4, "equal adjacent tiles merge");
  assert.equal(manager.score, 4, "a merge adds the merged tile value");
  assert.equal(nonEmptyCount(manager.grid), 2, "an effective move adds one tile");
  assert.equal(memory.bestScore, 4, "best score records the merge");
  assert.equal(lastMetadata(actuator).score, 4, "actuator receives the score");
}

function testBaselineNoOp(context) {
  const harness = makeHarness(context, 4);
  const { manager, actuator } = harness;
  actuator.calls.length = 0;
  installBoard(context, manager, 4, [[0, 0, 8]]);
  resetManager(manager);
  const before = plain(manager.serialize());

  manager.move(0); // already at the upper edge

  assert.deepEqual(plain(manager.serialize()), before, "a no-op leaves game state unchanged");
  assert.equal(actuator.calls.length, 0, "a no-op does not actuate");
}

function testBaselineRestore(context) {
  const saved = gameState(4, [[1, 2, 16]], {
    score: 12,
    won: true,
    keepPlaying: true
  });
  const harness = makeHarness(context, 4, saved, 20);
  const { manager } = harness;
  const actual = plain(manager.serialize());

  assert.deepEqual(actual.grid, saved.grid, "saved board is restored");
  assert.equal(actual.score, 12, "saved score is restored");
  assert.equal(actual.won, true, "saved win flag is restored");
  assert.equal(actual.keepPlaying, true, "saved keep-playing flag is restored");
}

function runBaseline(context) {
  testBaselineMerge(context);
  testBaselineNoOp(context);
  testBaselineRestore(context);
}

function testUndoRestoresMove(context) {
  const harness = makeHarness(context, 4);
  const { manager, memory } = harness;
  installBoard(context, manager, 4, [[1, 0, 2], [2, 0, 2]]);
  resetManager(manager, 10);
  const before = plain(manager.serialize());

  manager.move(3);
  const after = plain(manager.serialize());
  assert.equal(after.score, 14, "fixture makes an effective merge");
  assert.notDeepEqual(after.grid, before.grid, "move changes the board");
  assert.ok(memory.bestScore >= 14, "effective move updates best score");

  assert.equal(typeof manager.undo, "function", "GameManager exposes undo");
  assert.equal(manager.undo(), true, "undo reports that it restored a move");
  assert.deepEqual(plain(manager.serialize()), before, "undo restores board, score, and flags");
  assert.ok(memory.bestScore >= 14, "undo never lowers best score");

  const restored = plain(manager.serialize());
  assert.equal(manager.undo(), false, "only one undo is available");
  assert.deepEqual(plain(manager.serialize()), restored, "second undo is a no-op");
}

function testUndoRestoresFlags(context) {
  const wonHarness = makeHarness(context, 4);
  const wonManager = wonHarness.manager;
  installBoard(context, wonManager, 4, [[1, 0, 1024], [2, 0, 1024]]);
  resetManager(wonManager);
  const beforeWon = plain(wonManager.serialize());
  wonManager.move(3);
  assert.equal(wonManager.won, true, "2048 merge sets won");
  assert.equal(wonManager.undo(), true, "won move can be undone");
  assert.deepEqual(plain(wonManager.serialize()), beforeWon, "undo restores won flag");

  const overHarness = makeHarness(context, 4);
  const overManager = overHarness.manager;
  const placements = [];
  let exponent = 3;
  for (let x = 0; x < 4; x += 1) {
    for (let y = 0; y < 4; y += 1) {
      if (x !== 0 || y !== 0) {
        placements.push([x, y, 2 ** exponent]);
        exponent += 1;
      }
    }
  }
  installBoard(context, overManager, 4, placements);
  resetManager(overManager);
  const beforeOver = plain(overManager.serialize());
  overManager.move(0); // fills the only hole, then the unique board is stuck
  assert.equal(overManager.over, true, "an effective move can set game over");
  assert.equal(overManager.undo(), true, "game-over move can be undone");
  assert.deepEqual(plain(overManager.serialize()), beforeOver, "undo restores over flag");

  const keepHarness = makeHarness(context, 4);
  const keepManager = keepHarness.manager;
  keepHarness.manager.addRandomTile = function () {};
  installBoard(context, keepManager, 4, [[1, 0, 2]]);
  resetManager(keepManager);
  const beforeKeep = plain(keepManager.serialize());
  keepManager.move(3);
  keepHarness.input.emit("keepPlaying");
  assert.equal(keepManager.keepPlaying, true, "fixture changes keep-playing state after the move");
  assert.equal(keepManager.undo(), true, "undo restores keep-playing state");
  assert.deepEqual(plain(keepManager.serialize()), beforeKeep, "undo restores keepPlaying flag");
}

function testUndoNoOpAndRestart(context) {
  const noOpHarness = makeHarness(context, 4);
  const noOpManager = noOpHarness.manager;
  noOpManager.addRandomTile = function () {}; // keep the follow-up no-op deterministic
  installBoard(context, noOpManager, 4, [[2, 0, 2]]);
  resetManager(noOpManager);
  const beforeEffective = plain(noOpManager.serialize());
  noOpManager.move(3);
  const afterEffective = plain(noOpManager.serialize());
  assert.notDeepEqual(afterEffective.grid, beforeEffective.grid, "fixture makes an effective move");
  noOpManager.move(3); // the tile is now at the left edge
  assert.deepEqual(plain(noOpManager.serialize()), afterEffective, "no-op does not change state");
  assert.equal(noOpManager.undo(), true, "undo remains available after a no-op");
  assert.deepEqual(plain(noOpManager.serialize()), beforeEffective, "no-op does not overwrite undo snapshot");

  const restartHarness = makeHarness(context, 4);
  const restartManager = restartHarness.manager;
  installBoard(context, restartManager, 4, [[1, 0, 2], [2, 0, 2]]);
  resetManager(restartManager);
  restartManager.move(3);
  assert.ok(restartHarness.memory.bestScore >= 4, "restart fixture records best score");
  restartManager.restart();
  assert.equal(restartManager.undo(), false, "restart clears undo state");
  assert.ok(restartHarness.memory.bestScore >= 4, "restart preserves best score");
}

function runUndo(context) {
  testUndoRestoresMove(context);
  testUndoRestoresFlags(context);
  testUndoNoOpAndRestart(context);
}

function testMoveCount(context) {
  const harness = makeHarness(context, 4);
  const { manager, actuator } = harness;
  assert.equal(manager.moveCount, 0, "moveCount starts at zero");
  manager.addRandomTile = function () {};
  installBoard(context, manager, 4, [[2, 0, 2]]);
  resetManager(manager);
  actuator.calls.length = 0;

  manager.move(3);
  assert.equal(manager.moveCount, 1, "one effective move increments moveCount once");
  assert.equal(plain(manager.serialize()).moveCount, 1, "moveCount is serialized");
  assert.equal(lastMetadata(actuator).moveCount, 1, "actuator metadata includes moveCount");
  const callCount = actuator.calls.length;

  manager.move(3); // no-op at left edge
  assert.equal(manager.moveCount, 1, "no-op does not increment moveCount");
  assert.equal(actuator.calls.length, callCount, "no-op does not actuate");

  const saved = plain(manager.serialize());
  const restoredHarness = makeHarness(context, 4, saved);
  assert.equal(restoredHarness.manager.moveCount, 1, "moveCount restores from saved state");
  assert.equal(restoredHarness.actuator.calls[0].metadata.moveCount, 1, "restored metadata includes moveCount");

  const legacy = clone(saved);
  delete legacy.moveCount;
  const legacyHarness = makeHarness(context, 4, legacy);
  assert.equal(legacyHarness.manager.moveCount, 0, "legacy saves default missing moveCount to zero");

  manager.restart();
  assert.equal(manager.moveCount, 0, "restart resets moveCount");
  assert.equal(plain(manager.serialize()).moveCount, 0, "restart serializes moveCount zero");
  assert.equal(lastMetadata(actuator).moveCount, 0, "restart metadata includes zero moveCount");
}

function runMoves(context) {
  testMoveCount(context);
}

function fillGrid(context, size, emptyCell, valueFor) {
  const grid = new context.Grid(size);
  let index = 0;
  for (let x = 0; x < size; x += 1) {
    for (let y = 0; y < size; y += 1) {
      if (emptyCell && x === emptyCell[0] && y === emptyCell[1]) continue;
      grid.insertTile(new context.Tile({ x, y }, valueFor(index, x, y)));
      index += 1;
    }
  }
  return grid;
}

function testAnalysis(context) {
  const stats = new context.Grid(4);
  assert.equal(stats.maxTileValue(), 0, "empty grid maximum is zero");
  assert.equal(stats.emptyCellCount(), 16, "empty grid has sixteen empty cells");
  stats.insertTile(new context.Tile({ x: 0, y: 0 }, 8));
  stats.insertTile(new context.Tile({ x: 1, y: 2 }, 32));
  stats.insertTile(new context.Tile({ x: 3, y: 3 }, 16));
  const statsBefore = plain(stats.serialize());
  assert.equal(stats.maxTileValue(), 32, "maximum tile value is reported");
  assert.equal(stats.emptyCellCount(), 13, "empty cell count tracks occupied cells");
  assert.deepEqual(plain(stats.serialize()), statsBefore, "analysis helpers do not mutate the grid");

  const slideOnly = fillGrid(context, 4, [0, 0], (index) => 2 ** (index + 3));
  const slideBefore = plain(slideOnly.serialize());
  assert.deepEqual(plain(slideOnly.availableDirections()), [0, 3], "directions reflect slides toward the corner hole");
  assert.deepEqual(plain(slideOnly.serialize()), slideBefore, "availableDirections does not mutate slides");

  const equalPair = fillGrid(context, 4, null, (index) => 2 ** (index + 3));
  equalPair.removeTile(equalPair.cellContent({ x: 1, y: 0 }));
  equalPair.insertTile(new context.Tile({ x: 1, y: 0 }, 2));
  equalPair.removeTile(equalPair.cellContent({ x: 0, y: 0 }));
  equalPair.insertTile(new context.Tile({ x: 0, y: 0 }, 2));
  const equalBefore = plain(equalPair.serialize());
  assert.deepEqual(plain(equalPair.availableDirections()), [1, 3], "directions include both sides of an equal pair");
  assert.deepEqual(plain(equalPair.serialize()), equalBefore, "availableDirections does not mutate merges");

  const stuck = fillGrid(context, 4, null, (index) => 2 ** (index + 3));
  assert.deepEqual(plain(stuck.availableDirections()), [], "full board with no matches has no directions");
}

function runAnalysis(context) {
  testAnalysis(context);
}

function expectRejected(storage, label, value, raw = JSON.stringify(value)) {
  storage.storage.removeItem(storage.gameStateKey);
  storage.storage.setItem(storage.gameStateKey, raw);
  let result;
  assert.doesNotThrow(() => {
    result = storage.getGameState();
  }, `${label}: getGameState must not throw`);
  assert.equal(result, null, `${label}: malformed state is rejected`);
  assert.equal(storage.storage.getItem(storage.gameStateKey), undefined, `${label}: bad state is cleared`);
}

function testStorage(context) {
  const storage = new context.LocalStorageManager();
  storage.storage.clear();

  expectRejected(storage, "malformed JSON", null, "{");

  const valid = gameState(4, [[1, 2, 8]], { score: 12 });
  storage.storage.setItem(storage.gameStateKey, JSON.stringify(valid));
  let restored;
  assert.doesNotThrow(() => {
    restored = storage.getGameState();
  }, "legacy valid save must be readable");
  assert.deepEqual(plain(restored.grid), valid.grid, "legacy board remains readable");
  assert.equal(restored.score, 12, "legacy score remains readable");
  assert.equal(restored.moveCount, undefined, "legacy save need not contain moveCount");

  const mutate = (callback) => {
    const state = gameState(4, [[1, 2, 8]], { score: 12 });
    callback(state);
    return state;
  };

  const shapeCases = [
    ["null root", null],
    ["array root", []],
    ["string root", "state"],
    ["number root", 4],
    ["missing grid", mutate((s) => { delete s.grid; })],
    ["array grid", mutate((s) => { s.grid = []; })],
    ["missing size", mutate((s) => { delete s.grid.size; })],
    ["zero size", mutate((s) => { s.grid.size = 0; })],
    ["negative size", mutate((s) => { s.grid.size = -1; })],
    ["fractional size", mutate((s) => { s.grid.size = 2.5; })],
    ["string size", mutate((s) => { s.grid.size = "4"; })],
    ["missing cells", mutate((s) => { delete s.grid.cells; })],
    ["object cells", mutate((s) => { s.grid.cells = {}; })],
    ["short cells", mutate((s) => { s.grid.cells = s.grid.cells.slice(1); })],
    ["object row", mutate((s) => { s.grid.cells[0] = {}; })],
    ["short row", mutate((s) => { s.grid.cells[0] = s.grid.cells[0].slice(1); })]
  ];
  for (const [label, value] of shapeCases) expectRejected(storage, label, value);

  const tileCases = [
    ["tile missing position", { value: 8 }],
    ["tile missing value", { position: { x: 1, y: 2 } }],
    ["tile primitive", 8],
    ["null position", { position: null, value: 8 }],
    ["mismatched x", { position: { x: 0, y: 2 }, value: 8 }],
    ["mismatched y", { position: { x: 1, y: 1 }, value: 8 }],
    ["fractional coordinate", { position: { x: 1.5, y: 2 }, value: 8 }],
    ["out-of-range coordinate", { position: { x: 4, y: 2 }, value: 8 }],
    ["value one", { position: { x: 1, y: 2 }, value: 1 }],
    ["non-power value", { position: { x: 1, y: 2 }, value: 6 }],
    ["negative value", { position: { x: 1, y: 2 }, value: -2 }],
    ["string value", { position: { x: 1, y: 2 }, value: "8" }],
    ["null value", { position: { x: 1, y: 2 }, value: null }]
  ];
  for (const [label, tile] of tileCases) {
    expectRejected(storage, label, mutate((s) => { s.grid.cells[1][2] = tile; }));
  }

  const scoreCases = [
    ["negative score", -1],
    ["string score", "12"],
    ["null score", null]
  ];
  for (const [label, score] of scoreCases) {
    expectRejected(storage, label, mutate((s) => { s.score = score; }));
  }

  for (const flag of ["over", "won", "keepPlaying"]) {
    for (const bad of [null, 0, "false"]) {
      expectRejected(storage, `${flag}=${String(bad)}`, mutate((s) => { s[flag] = bad; }));
    }
  }
}

function runStorage(context) {
  testStorage(context);
}

function testUndoAndCountIntegration(context) {
  const saved = gameState(4, [[1, 0, 2], [2, 0, 2]], { moveCount: 7 });
  const harness = makeHarness(context, 4, saved);
  const { manager } = harness;
  const before = plain(manager.serialize());
  assert.equal(before.moveCount, 7, "integration fixture restores prior count");
  manager.move(3);
  assert.equal(manager.moveCount, 8, "effective move increments restored count once");
  assert.equal(manager.undo(), true, "integration move can be undone");
  assert.deepEqual(plain(manager.serialize()), before, "undo restores the prior moveCount with the state");
}

function parseGroups(argv) {
  const raw = argv[3];
  if (!raw) throw new Error("usage: node acceptance.cjs REPO_DIR GROUP");
  const requested = argv.slice(3).join(",").split(/[,+]/).map((group) => group.trim()).filter(Boolean);
  if (requested.includes("all")) return [...GROUPS, "integration"];
  for (const group of requested) {
    if (!GROUPS.includes(group)) throw new Error(`unknown GROUP '${group}'`);
  }
  return [...new Set(requested)];
}

function main() {
  const repoDir = path.resolve(process.argv[2] || "");
  const groups = parseGroups(process.argv);
  const context = loadGame(repoDir);
  const runners = {
    baseline: runBaseline,
    undo: runUndo,
    moves: runMoves,
    analysis: runAnalysis,
    storage: runStorage,
    integration: testUndoAndCountIntegration
  };
  for (const group of groups) {
    runners[group](context);
    process.stdout.write(`PASS ${group}\n`);
  }
}

main();

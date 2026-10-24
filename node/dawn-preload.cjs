// Gives a WebAssembly build running under node a navigator.gpu backed by Dawn
// (dawn.node from the `webgpu` npm package), as a browser would. Preloaded with
// `node -r`, which worker threads inherit, so the WebGPU device thread's worker
// gets it too. `rex wasm dawn` sets REX_DAWN_NODE to the addon.
"use strict";
const path = process.env.REX_DAWN_NODE;
if (!path) {
  throw new Error("dawn-preload: set REX_DAWN_NODE to dawn.node");
}
const { create, globals } = require(path);
Object.assign(globalThis, globals);
const options = (process.env.REX_DAWN_OPTIONS || "").split(";").filter(Boolean);
const gpu = create(options);
if (globalThis.navigator) {
  Object.defineProperty(globalThis.navigator, "gpu", { value: gpu, configurable: true });
} else {
  globalThis.navigator = { gpu };
}

// Emulate touch capability for paired controller behavior in desktop Chromium.
Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => 5 });
window.ontouchstart = null;

"use strict";

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../frontend/static/js/constellation.js'), 'utf8');

function harness(reduced = false, canvasAvailable = true) {
    const listeners = new Map(), frames = new Map(), arcs = [], labels = [];
    let sequence = 0, random = 42;
    const listen = (name, handler, options) => {
        listeners.set(name, handler);
        if (options) options.signal.addEventListener('abort', () => listeners.delete(name));
    };
    const context = {
        setTransform() {}, clearRect() { arcs.length = 0; labels.length = 0; }, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() {}, fill() {}, roundRect() {}, save() {}, restore() {}, setLineDash() {},
        fillText(text) { labels.push(text); },
        arc(x, y, radius) { arcs.push(radius); }, measureText() { return {width: 60}; },
        createRadialGradient() { return {addColorStop() {}}; }
    };
    const canvas = {getContext() { return canvasAvailable ? context : null; }};
    const document = {hidden: false, getElementById() { return canvas; }, addEventListener: listen};
    const motion = {matches: reduced, addEventListener: listen};
    const math = Object.create(Math);
    math.random = () => { random = (random * 1664525 + 1013904223) >>> 0; return random / 4294967296; };
    vm.runInNewContext(code, {
        document, matchMedia() { return motion; }, AbortController, Math: math,
        innerWidth: 1920, innerHeight: 1080, devicePixelRatio: 3,
        addEventListener: listen,
        requestAnimationFrame(handler) { frames.set(++sequence, handler); return sequence; },
        cancelAnimationFrame(id) { frames.delete(id); }
    });
    return {canvas, document, motion, listeners, frames, arcs, labels,
        advance() {
            const [id, callback] = frames.entries().next().value;
            frames.delete(id); callback(1000);
        }};
}

const running = harness();
assert.equal(running.frames.size, 1);
assert.equal(running.canvas.width, 3840); // Pixel ratio capped at two.
for (let i = 0; i < 50; i++) running.listeners.get('pointerdown')({clientX: 10, clientY: 10});
running.advance();
assert.equal(running.arcs.filter(radius => radius === 8).length, 12);
assert.equal(running.frames.size, 1);
running.document.hidden = true;
running.listeners.get('visibilitychange')();
assert.equal(running.frames.size, 0);
running.document.hidden = false;
running.listeners.get('visibilitychange')();
assert.equal(running.frames.size, 1);
running.motion.matches = true;
running.listeners.get('change')();
assert.equal(running.frames.size, 0);
running.listeners.get('pagehide')({persisted: false});
assert.equal(running.listeners.size, 0);

const still = harness(true);
assert.equal(still.frames.size, 0);
assert.ok(still.arcs.length > 0);
assert.ok(still.labels.length > 0);
assert.ok(still.labels.every(label => /^(5[5-9]|[6-8]\d|9[0-7])% match$/.test(label)));
still.listeners.get('resize')();
assert.equal(still.frames.size, 0);

const unsupported = harness(false, false);
assert.equal(unsupported.canvas.hidden, true);
assert.equal(unsupported.frames.size, 0);
assert.equal(unsupported.listeners.size, 0);

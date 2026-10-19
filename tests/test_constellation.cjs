"use strict";

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../frontend/static/js/constellation.js'), 'utf8');

function harness(reduced = false, canvasAvailable = true, boxes = [], textBoxes = []) {
    const listeners = new Map(), frames = new Map(), arcs = [], labels = [], labelPositions = [];
    let sequence = 0, random = 42, now = 0;
    const listen = (name, handler, options) => {
        listeners.set(name, handler);
        if (options && options.signal) options.signal.addEventListener('abort', () => listeners.delete(name));
    };
    const context = {
        setTransform() {}, clearRect() { arcs.length = 0; labels.length = 0; labelPositions.length = 0; }, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() {}, fill() {}, roundRect() {}, save() {}, restore() {}, setLineDash() {},
        fillText(text, x, y) { labels.push(text); labelPositions.push({x, y}); },
        arc(x, y, radius) { arcs.push(radius); }, measureText() { return {width: 60}; },
        createRadialGradient() { return {addColorStop() {}}; }
    };
    const canvas = {getContext() { return canvasAvailable ? context : null; }};
    const document = {
        hidden: false, body: {}, getElementById() { return canvas; }, addEventListener: listen,
        querySelectorAll() {
            return boxes.map(box => ({closest() { return box.ignored; }, getBoundingClientRect() { return box; }}));
        },
        createTreeWalker(body, type, filter) {
            let index = 0;
            return {currentNode: null, nextNode() {
                while (index < textBoxes.length) {
                    const box = textBoxes[index++];
                    const parent = {tagName: box.tagName || 'P', closest() { return box.ignored; }};
                    const node = {nodeValue: box.text === undefined ? 'Page text' : box.text,
                        parentNode: parent, parentElement: parent, box};
                    if (filter.acceptNode(node) === 1) { this.currentNode = node; return true; }
                }
                return false;
            }};
        },
        createRange() {
            let node;
            return {selectNodeContents(value) { node = value; }, getClientRects() { return [node.box]; }};
        }
    };
    const motion = {matches: reduced, addEventListener: listen};
    const math = Object.create(Math);
    math.random = () => { random = (random * 1664525 + 1013904223) >>> 0; return random / 4294967296; };
    vm.runInNewContext(code, {
        document, matchMedia() { return motion; }, AbortController, Math: math,
        NodeFilter: {SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2},
        innerWidth: 1920, innerHeight: 1080, devicePixelRatio: 3,
        addEventListener: listen,
        requestAnimationFrame(handler) { frames.set(++sequence, handler); return sequence; },
        cancelAnimationFrame(id) { frames.delete(id); }
    });
    return {canvas, document, motion, listeners, frames, arcs, labels, labelPositions,
        advance() {
            const [id, callback] = frames.entries().next().value;
            frames.delete(id); now += 1000 / 60; callback(now);
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

function rectangle(left, top, right, bottom, properties = {}) {
    return {left, top, right, bottom, width: right - left, height: bottom - top, ...properties};
}

const fullPage = rectangle(0, 0, 1920, 1080);
assert.equal(harness(true, true, [fullPage]).labels.length, 0);
assert.equal(harness(true, true, [], [fullPage]).labels.length, 0);
assert.ok(harness(true, true, [{...fullPage, ignored: true}]).labels.length > 0);
assert.ok(harness(true, true, [], [{...fullPage, ignored: true}]).labels.length > 0);
assert.ok(harness(true, true, [], [{...fullPage, tagName: 'SCRIPT'}]).labels.length > 0);
assert.ok(harness(true, true, [], [{...fullPage, text: '  '}]).labels.length > 0);
assert.ok(harness(true, true, [rectangle(0, 0, 0, 0)]).labels.length > 0);
assert.ok(harness(true, true, [rectangle(0, 1200, 1920, 1400)]).labels.length > 0);

// Cover the above-node position and verify the same node's label moves below it.
const firstLabel = harness(true).labelPositions[0];
const above = rectangle(firstLabel.x - 38, firstLabel.y - 13, firstLabel.x + 38, firstLabel.y + 5);
const below = harness(true, true, [above]);
assert.ok(below.labelPositions.some(label => label.x === firstLabel.x && label.y > firstLabel.y + 38));

// Content entering the viewport invalidates labels, including in reduced motion.
const dynamicBoxes = [];
const dynamic = harness(true, true, dynamicBoxes);
assert.ok(dynamic.labels.length > 0);
dynamicBoxes.push(fullPage);
dynamic.listeners.get('scroll')();
assert.equal(dynamic.labels.length, 0);
assert.equal(dynamic.frames.size, 0);
dynamicBoxes.pop();
dynamic.listeners.get('resize')();
assert.ok(dynamic.labels.length > 0);

const movingBoxes = [];
const moving = harness(false, true, movingBoxes);
for (let index = 0; index < 30; index++) moving.advance();
assert.ok(moving.labels.length > 0);
movingBoxes.push(fullPage);
for (let index = 0; index < 60; index++) moving.advance();
assert.equal(moving.labels.length, 0);

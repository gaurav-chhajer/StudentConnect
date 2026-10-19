"use strict";

// Adapted from the preserved Match Constellation Background design.
// Decorative signals only: no account, resume or match data enters this canvas.
(() => {
    const canvas = document.getElementById('match-constellation');
    if (!canvas) return;
    const context = canvas.getContext('2d');
    // The CSS gradient remains the background when canvas is unavailable.
    if (!context) { canvas.hidden = true; return; }

    const MAX_NODES = 90, MAX_RIPPLES = 12, SCAN_RADIUS = 185;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const owner = new AbortController();
    const options = {signal: owner.signal};
    let width = 0, height = 0, nodes = [], ripples = [], frameId = null;
    let tick = 0, previous = null, pointer = {x: 0, y: 0, active: false};
    let avoid = [], avoidDirty = true, collectedAt = -30;

    function collectAvoidance() {
        avoid = []; avoidDirty = false; collectedAt = tick;
        function collect(rectangles) {
            for (const rect of rectangles) {
                if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < height) avoid.push(rect);
            }
        }
        for (const element of document.querySelectorAll('button,input,select,textarea,.card,[data-bg-avoid]')) {
            if (!element.closest('[data-bg-ignore]')) collect([element.getBoundingClientRect()]);
        }
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                return node.nodeValue.trim() && !/^(SCRIPT|STYLE|NOSCRIPT)$/.test(node.parentNode.tagName) ?
                    NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
            }
        });
        const range = document.createRange();
        while (walker.nextNode()) {
            if (walker.currentNode.parentElement.closest('[data-bg-ignore]')) continue;
            range.selectNodeContents(walker.currentNode);
            collect(range.getClientRects());
        }
    }

    function hitsContent(x, y, boxWidth, boxHeight) {
        return avoid.some(rect => x < rect.right + 6 && x + boxWidth > rect.left - 6 &&
            y < rect.bottom + 4 && y + boxHeight > rect.top - 4);
    }

    function labelTop(node) {
        return node.labelDirection < 0 ? node.y - node.radius - 30 : node.y + node.radius + 8;
    }

    function labelHitsContent(node) {
        context.font = '600 11px system-ui,sans-serif';
        const labelWidth = context.measureText(node.score + '% match').width + 14;
        return hitsContent(node.x - labelWidth / 2, labelTop(node), labelWidth, 18);
    }

    function resize() {
        width = innerWidth; height = innerHeight;
        const ratio = Math.min(devicePixelRatio || 1, 2);
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        const count = Math.max(36, Math.min(MAX_NODES, Math.round(width * height / 17000)));
        nodes = Array.from({length: count}, () => ({
            x: Math.random() * width, y: Math.random() * height,
            vx: (Math.random() - .5) * .3, vy: (Math.random() - .5) * .3,
            score: 55 + Math.floor(Math.random() * 43), radius: 3 + Math.random() * 3, flash: 0,
            label: false, labelSince: 0, cooldown: 0, alpha: 0, labelDirection: -1, obscured: 0
        }));
        ripples = [];
        avoidDirty = true;
        if (motion.matches) draw(0);
    }

    function draw(step) {
        tick += step;
        if (avoidDirty || tick - collectedAt >= 30) collectAvoidance();
        context.clearRect(0, 0, width, height);
        const x = pointer.active ? pointer.x : width / 2 + Math.cos(tick * .006) * width * .3;
        const y = pointer.active ? pointer.y : height / 2 + Math.sin(tick * .009) * height * .25;
        for (const node of nodes) {
            node.x += node.vx * step; node.y += node.vy * step;
            if (node.x < -10) node.x = width + 10;
            if (node.x > width + 10) node.x = -10;
            if (node.y < -10) node.y = height + 10;
            if (node.y > height + 10) node.y = -10;
            const dx = x - node.x, dy = y - node.y;
            node.distance = Math.hypot(dx, dy);
            if (node.distance < SCAN_RADIUS && node.distance > 1) {
                const pull = .35 * (1 - node.distance / SCAN_RADIUS) * step;
                node.x += dx / node.distance * pull;
                node.y += dy / node.distance * pull;
            }
            node.flash = Math.max(0, node.flash - .015 * step);
        }
        for (const ripple of ripples) {
            ripple.radius += 8 * step;
            for (const node of nodes) {
                if (Math.abs(Math.hypot(node.x - ripple.x, node.y - ripple.y) - ripple.radius) < 12) node.flash = 1;
            }
        }
        ripples = ripples.filter(ripple => ripple.radius < 320);

        function line(ax, ay, bx, by, color) {
            context.strokeStyle = color; context.lineWidth = 1;
            context.beginPath(); context.moveTo(ax, ay); context.lineTo(bx, by); context.stroke();
        }
        for (let first = 0; first < nodes.length; first++) {
            for (let second = first + 1; second < nodes.length; second++) {
                const a = nodes[first], b = nodes[second], distance = Math.hypot(a.x - b.x, a.y - b.y);
                if (distance < 125) line(a.x, a.y, b.x, b.y, `rgba(47,75,234,${(1 - distance / 125) * .14})`);
            }
        }
        for (const ripple of ripples) {
            context.strokeStyle = `rgba(255,122,89,${(1 - ripple.radius / 320) * .5})`;
            context.lineWidth = 2; context.beginPath();
            context.arc(ripple.x, ripple.y, ripple.radius, 0, Math.PI * 2); context.stroke();
        }
        for (const node of nodes) {
            const boost = Math.max(node.flash, 1 - node.distance / SCAN_RADIUS, 0);
            node.obscured += ((hitsContent(node.x - 12, node.y - 12, 24, 24) ? 1 : 0) - node.obscured) *
                (motion.matches ? 1 : .12 * step);
            context.globalAlpha = 1 - .85 * node.obscured;
            const color = node.score >= 85 ? '235,95,60' : node.score >= 70 ? '47,75,234' : '30,160,125';
            const radius = node.radius * (1 + boost * .7);
            if (node.distance < SCAN_RADIUS) line(x, y, node.x, node.y, `rgba(255,122,89,${boost * .4})`);
            if (node.score >= 85 || boost > .2) {
                const glow = context.createRadialGradient(node.x, node.y, 0, node.x, node.y, radius * 5);
                glow.addColorStop(0, `rgba(${color},${.28 + boost * .3})`);
                glow.addColorStop(1, `rgba(${color},0)`);
                context.fillStyle = glow; context.beginPath();
                context.arc(node.x, node.y, radius * 5, 0, Math.PI * 2); context.fill();
            }
            context.fillStyle = `rgba(${color},${.5 + boost * .5})`;
            context.beginPath();
            context.roundRect(node.x - radius, node.y - radius, radius * 2, radius * 2, radius * .6);
            context.fill();
            context.globalAlpha = 1;
            if (node.label && (node.distance > SCAN_RADIUS * .9 || tick - node.labelSince > 240 || labelHitsContent(node))) {
                node.label = false; node.cooldown = tick + 150;
            }
        }
        context.save(); context.strokeStyle = 'rgba(14,21,48,.14)'; context.lineWidth = 1;
        context.setLineDash([4, 8]); context.lineDashOffset = -tick * .4;
        context.beginPath(); context.arc(x, y, SCAN_RADIUS, 0, Math.PI * 2); context.stroke(); context.restore();

        let labels = nodes.filter(node => node.label).length;
        for (const node of nodes) {
            if (labels < 3 && !node.label && node.cooldown <= tick && node.distance < SCAN_RADIUS * .65 &&
                !nodes.some(other => other.label && Math.hypot(other.x - node.x, other.y - node.y) < 90)) {
                node.labelDirection = -1;
                if (labelHitsContent(node)) {
                    node.labelDirection = 1;
                    if (labelHitsContent(node)) continue;
                }
                node.label = true; node.labelSince = tick; labels++;
            }
            node.alpha += ((node.label ? 1 : 0) - node.alpha) * (motion.matches ? 1 : (node.label ? .1 : .25) * step);
            if (node.alpha > .02) {
                const text = node.score + '% match';
                context.globalAlpha = node.alpha * .8; context.font = '600 11px system-ui,sans-serif';
                const pillWidth = context.measureText(text).width + 14;
                context.fillStyle = 'rgba(47,75,234,.92)'; context.beginPath();
                const top = labelTop(node);
                context.roundRect(node.x - pillWidth / 2, top, pillWidth, 18, 9); context.fill();
                context.fillStyle = '#fff'; context.textAlign = 'center'; context.fillText(text, node.x, top + 13);
                context.globalAlpha = 1;
            }
        }
    }

    function frame(now) {
        frameId = null;
        if (document.hidden || motion.matches) return;
        const step = previous === null ? 1 : Math.min((now - previous) / (1000 / 60), 2);
        previous = now; draw(step); frameId = requestAnimationFrame(frame);
    }
    function synchronize() {
        if (frameId !== null) cancelAnimationFrame(frameId);
        frameId = null; previous = null;
        if (!document.hidden) {
            if (motion.matches) draw(0);
            else frameId = requestAnimationFrame(frame);
        }
    }
    addEventListener('pointermove', event => {
        pointer = {x: event.clientX, y: event.clientY, active: event.pointerType === 'mouse'};
    }, options);
    addEventListener('pointerdown', event => {
        if (motion.matches) return;
        if (ripples.length >= MAX_RIPPLES) ripples.shift();
        ripples.push({x: event.clientX, y: event.clientY, radius: 0});
    }, options);
    document.addEventListener('pointerleave', () => { pointer.active = false; }, options);
    addEventListener('resize', resize, options);
    addEventListener('scroll', () => {
        avoidDirty = true;
        if (motion.matches && !document.hidden) draw(0);
    }, {signal: owner.signal, passive: true, capture: true});
    document.addEventListener('visibilitychange', synchronize, options);
    motion.addEventListener('change', synchronize, options);
    addEventListener('pageshow', synchronize, options);
    addEventListener('pagehide', event => {
        if (frameId !== null) cancelAnimationFrame(frameId);
        frameId = null;
        if (!event.persisted) owner.abort();
    }, options);
    resize(); synchronize();
})();

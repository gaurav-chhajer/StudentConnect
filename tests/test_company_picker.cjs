"use strict";

const assert = require("node:assert/strict");

function element(properties = {}) {
    return Object.assign({
        hidden: true,
        handlers: {},
        attributes: {},
        addEventListener(name, handler) { this.handlers[name] = handler; },
        setAttribute(name, value) { this.attributes[name] = value; }
    }, properties);
}

const rows = Array.from({length: 30}, (_, index) => element({
    hidden: false,
    dataset: {companyName: index === 29 ? 'Cvent' : 'Company ' + index},
    checkbox: {checked: index === 29, disabled: index === 28}
}));
const elements = {
    'company-search-controls': element(),
    'company-search': element({value: ''}),
    'company-search-status': element(),
    'company-view-more': element(),
    'company-options': element({querySelectorAll() { return rows; }})
};
global.document = {getElementById(id) { return elements[id]; }};
require('../frontend/static/js/companies.js');

const input = elements['company-search'];
const more = elements['company-view-more'];
const status = elements['company-search-status'];
const visibleCount = () => rows.filter(row => !row.hidden).length;
assert.equal(elements['company-search-controls'].hidden, false);
assert.equal(visibleCount(), 12);
more.handlers.click();
assert.equal(visibleCount(), 24);
more.handlers.click();
assert.equal(visibleCount(), 30);
assert.equal(more.textContent, 'View fewer');
assert.equal(more.attributes['aria-expanded'], 'true');
more.handlers.click();
assert.equal(visibleCount(), 12);
input.value = '  CVEnt  ';
input.handlers.input();
assert.equal(visibleCount(), 1);
assert.equal(rows[29].hidden, false);
assert.equal(rows[29].checkbox.checked, true);
assert.equal(more.hidden, true);
input.value = '<script>';
input.handlers.input();
assert.equal(visibleCount(), 0);
assert.equal(status.textContent, 'No companies found. Try another name.');
input.value = '';
input.handlers.input();
assert.equal(visibleCount(), 12);
assert.equal(rows[29].checkbox.checked, true);
assert.equal(rows[28].checkbox.disabled, true);
let prevented = false;
input.handlers.keydown({key: 'Enter', preventDefault() { prevented = true; }});
assert.equal(prevented, true);

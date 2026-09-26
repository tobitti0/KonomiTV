const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const ts = require('typescript');

// 既存の TypeScript と Node のテストランナーを使い、実際の編集処理を検証する。
// 型専用の import は変換時に消えるため、ブラウザーや API クライアントは起動しない。
const source = readFileSync(path.join(__dirname, '../src/utils/RecordingFolderSettings.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const exportsObject = {};
vm.runInThisContext(`(exports) => { ${compiled.outputText} }`)(exportsObject);
const { editPrimaryRecordingFolder } = exportsObject;

const linuxFolder = {
    recording_folder_path: '/recordings',
    write_plugin: 'Write_Default.so',
    recording_file_name_plugin: 'RecName_Macro.so',
    recording_file_name_template: '$title$.ts',
    is_oneseg_separate_recording_folder: false,
};

test('new folders inherit the exact Linux, Windows or custom plugin names', () => {
    for (const [writePlugin, namePlugin] of [
        ['Write_Default.so', 'RecName_Macro.so'],
        ['Write_Default.dll', 'RecName_Macro.dll'],
        ['Write_Custom.so', 'RecName_Custom.so'],
    ]) {
        const defaults = { ...linuxFolder, write_plugin: writePlugin, recording_file_name_plugin: namePlugin };
        const [result] = editPrimaryRecordingFolder([], defaults, '/new', '$title$.ts');
        assert.equal(result.write_plugin, writePlugin);
        assert.equal(result.recording_file_name_plugin, namePlugin);
    }
});

test('missing presets never create a folder with guessed plugins', () => {
    assert.equal(editPrimaryRecordingFolder([], undefined, '/new', ''), null);
    assert.equal(editPrimaryRecordingFolder([], undefined, '', '$title$.ts'), null);
    assert.deepEqual(editPrimaryRecordingFolder([], undefined, '', ''), []);
});

test('explicitly unspecified name plugin stays null when adding a folder', () => {
    const defaults = { ...linuxFolder, write_plugin: '', recording_file_name_plugin: null };
    const [result] = editPrimaryRecordingFolder([], defaults, '/new', '');
    assert.equal(result.write_plugin, '');
    assert.equal(result.recording_file_name_plugin, null);
    assert.equal(editPrimaryRecordingFolder([], defaults, '/new', '$title$.ts'), null);
});

test('editing works without presets and preserves additional and oneseg folders', () => {
    const oneseg = { ...linuxFolder, recording_folder_path: '/oneseg', is_oneseg_separate_recording_folder: true };
    const secondary = { ...linuxFolder, recording_folder_path: '/backup' };
    const folders = [oneseg, linuxFolder, secondary];
    const result = editPrimaryRecordingFolder(folders, undefined, '/new', '$title$?extra.ts');
    assert.deepEqual(result[0], oneseg);
    assert.deepEqual(result[2], secondary);
    assert.equal(result[1].recording_folder_path, '/new');
    assert.equal(result[1].recording_file_name_template, '$title$?extra.ts');
    assert.equal(linuxFolder.recording_folder_path, '/recordings');
});

test('clearing and re-entering a path retains selected custom plugin settings', () => {
    const custom = { ...linuxFolder, write_plugin: 'Write_Custom.so', recording_file_name_plugin: null };
    const oneseg = { ...linuxFolder, is_oneseg_separate_recording_folder: true };
    const cleared = editPrimaryRecordingFolder([custom, oneseg], linuxFolder, '', '');
    assert.equal(cleared.length, 2);
    const result = editPrimaryRecordingFolder(cleared, linuxFolder, '/again', '');
    assert.equal(result[0].write_plugin, 'Write_Custom.so');
    assert.equal(result[0].recording_file_name_plugin, null);
    assert.deepEqual(result[1], oneseg);
});

test('oneseg-only reservations gain a normal folder without changing oneseg settings', () => {
    const oneseg = { ...linuxFolder, is_oneseg_separate_recording_folder: true };
    assert.equal(editPrimaryRecordingFolder([oneseg], undefined, '/new', ''), null);
    const result = editPrimaryRecordingFolder([oneseg], linuxFolder, '/new', '');
    assert.equal(result[0].is_oneseg_separate_recording_folder, false);
    assert.deepEqual(result[1], oneseg);
});

test('path-only edits preserve an explicit empty plugin option', () => {
    const folder = { ...linuxFolder, recording_file_name_template: '' };
    const [result] = editPrimaryRecordingFolder([folder], undefined, '/new', '');
    assert.equal(result.recording_file_name_template, '');
});

import type { IRecordingFolder } from '@/services/Reservations';

/**
 * 通常録画の先頭フォルダを編集し、追加の保存先とワンセグ録画の設定を保持する。
 * フォルダ追加時は EDCB から取得したプラグインを継承し、設定が不明なら更新しない。
 * @param folders 現在の録画フォルダ一覧
 * @param defaultFolder EDCB のデフォルトプリセットの通常録画フォルダ
 * @param folderPath 新しい保存先
 * @param fileNameTemplate 新しいファイル名テンプレート
 * @returns 更新後の一覧、プラグイン設定が不明または未指定のプラグインへのマクロ指定なら null
 */
export function editPrimaryRecordingFolder(
    folders: IRecordingFolder[],
    defaultFolder: IRecordingFolder | undefined,
    folderPath: string,
    fileNameTemplate: string,
): IRecordingFolder[] | null {
    const index = folders.findIndex(folder => !folder.is_oneseg_separate_recording_folder);
    const source = index >= 0 ? folders[index] : defaultFolder;

    // 空欄のままならフォルダを作らず、EDCB のデフォルト保存先を使用する。
    if (index < 0 && folderPath.trim() === '' && fileNameTemplate.trim() === '') return [...folders];
    if (source === undefined) return null;
    if (!source.recording_file_name_plugin && fileNameTemplate.trim() !== '') return null;

    // 空文字列・null も EDCB の設定値なのでそのまま引き継ぐ。
    // パスを空欄に戻してもプラグイン設定や他の保存先を削除しない。
    const updated: IRecordingFolder = {
        ...source,
        recording_folder_path: folderPath,
        recording_file_name_template: index >= 0 && fileNameTemplate === (source.recording_file_name_template ?? '')
            ? source.recording_file_name_template
            : fileNameTemplate || null,
        is_oneseg_separate_recording_folder: false,
    };
    if (index < 0) return [updated, ...folders];
    return folders.map((folder, folderIndex) => folderIndex === index ? updated : folder);
}

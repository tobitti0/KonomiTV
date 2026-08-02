import APIClient from '@/services/APIClient';


/** KonomiTV の録画番組と Box TS の紐付け情報 */
export interface IBoxRecordedFileLink {
    recorded_program_id: number;
    box_file_id: string;
    name: string;
    size: number;
    availability: 'Available' | 'Missing' | 'Error';
    match_method: string;
    last_synced_at: string;
}


/** テスト・保守向けの Box 録画紐付け API */
class BoxRecordedFiles {

    /**
     * 録画番組の現在の紐付けを取得する。
     * Box API へはアクセスせず、KonomiTV 内の DB だけを参照する。
     */
    static async fetchLink(videoID: number): Promise<IBoxRecordedFileLink | null | undefined> {
        const response = await APIClient.get<IBoxRecordedFileLink | null>(`/extensions/box/recordings/${videoID}`);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'Box 録画の紐付け情報を取得できませんでした。');
            return undefined;
        }
        return response.data;
    }

    /** 入力された Box file ID を録画番組へ紐付ける。 */
    static async link(videoID: number, fileID: string): Promise<IBoxRecordedFileLink | null> {
        const response = await APIClient.put<IBoxRecordedFileLink>(
            `/extensions/box/recordings/${videoID}`,
            {file_id: fileID},
        );
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'Box 録画を紐付けできませんでした。');
            return null;
        }
        return response.data;
    }

    /** KonomiTV 内の紐付けだけを解除する。Box 上のファイルは削除しない。 */
    static async unlink(videoID: number): Promise<boolean> {
        const response = await APIClient.delete(`/extensions/box/recordings/${videoID}`);
        if (response.type === 'error') {
            APIClient.showGenericError(response, 'Box 録画の紐付けを解除できませんでした。');
            return false;
        }
        return true;
    }
}


export default BoxRecordedFiles;

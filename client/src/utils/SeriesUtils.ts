import { ISeries, ISeriesBroadcastPeriod } from '@/services/Series';
import { IRecordedProgram } from '@/services/Videos';
import { dayjs } from '@/utils';


/**
 * シリーズとエピソードの表示順・導線を一元管理するユーティリティ
 */
export class SeriesUtils {

    /**
     * 放送期間を新しい順に並べる
     * @param series シリーズ情報
     * @returns 新しい放送期間が先頭の配列
     */
    static getSortedBroadcastPeriods(series: ISeries): ISeriesBroadcastPeriod[] {
        return [...series.broadcast_periods].sort((a, b) => {
            return dayjs(b.start_date).valueOf() - dayjs(a.start_date).valueOf();
        });
    }


    /**
     * 放送期間内の録画番組を放送開始が古い順に並べる
     * @param broadcastPeriod 放送期間情報
     * @returns 放送開始が古い番組から並んだ配列
     */
    static getProgramsInBroadcastPeriod(broadcastPeriod: ISeriesBroadcastPeriod): IRecordedProgram[] {
        return [...broadcastPeriod.recorded_programs].sort((a, b) => {
            return dayjs(a.start_time).valueOf() - dayjs(b.start_time).valueOf();
        });
    }


    /**
     * シリーズ内の録画番組を重複なく放送開始が古い順に並べる
     * @param series シリーズ情報
     * @returns シリーズに属する全録画番組
     */
    static getAllPrograms(series: ISeries): IRecordedProgram[] {
        const programsById = new Map<number, IRecordedProgram>();
        for (const broadcastPeriod of series.broadcast_periods) {
            for (const program of broadcastPeriod.recorded_programs) {
                programsById.set(program.id, program);
            }
        }
        return [...programsById.values()].sort((a, b) => {
            return dayjs(a.start_time).valueOf() - dayjs(b.start_time).valueOf();
        });
    }


    /**
     * シリーズカードやヒーロー表示に利用する最新の録画番組を取得する
     * @param series シリーズ情報
     * @returns 最新の再生可能な録画番組。存在しなければ最新の録画番組、それもなければ null
     */
    static getRepresentativeProgram(series: ISeries): IRecordedProgram | null {
        const programs = SeriesUtils.getAllPrograms(series).reverse();
        return programs.find(program => program.recorded_video.status === 'Recorded') ?? programs[0] ?? null;
    }


    /**
     * 現在の録画番組と同じ放送期間内にある、次の再生可能な録画番組を取得する
     * @param series シリーズ情報
     * @param currentProgramId 現在再生中の録画番組 ID
     * @returns 次の録画番組。最終話またはシリーズ情報に存在しない場合は null
     */
    static getNextProgram(series: ISeries, currentProgramId: number): IRecordedProgram | null {
        const currentBroadcastPeriod = series.broadcast_periods.find((broadcastPeriod) => {
            return broadcastPeriod.recorded_programs.some(program => program.id === currentProgramId);
        });
        if (currentBroadcastPeriod === undefined) {
            return null;
        }

        const programs = SeriesUtils.getProgramsInBroadcastPeriod(currentBroadcastPeriod);
        const currentProgramIndex = programs.findIndex(program => program.id === currentProgramId);
        if (currentProgramIndex === -1) {
            return null;
        }

        // 録画中・解析失敗の番組は再生ページへ進めないため飛ばす
        return programs.slice(currentProgramIndex + 1).find((program) => {
            return program.recorded_video.status === 'Recorded';
        }) ?? null;
    }


    /**
     * エピソード一覧向けの短いタイトルを生成する
     * @param program 録画番組情報
     * @returns 話数とサブタイトルを組み合わせた表示名
     */
    static getEpisodeTitle(program: IRecordedProgram): string {
        const episodeNumber = program.episode_number !== null ? `#${program.episode_number}` : '';
        const subtitle = program.subtitle ?? program.title;
        return [episodeNumber, subtitle].filter(text => text !== '').join(' ');
    }


    /**
     * 放送期間選択欄向けのラベルを生成する
     * @param broadcastPeriod 放送期間情報
     * @returns チャンネル・期間・録画件数を含む表示名
     */
    static getBroadcastPeriodLabel(broadcastPeriod: ISeriesBroadcastPeriod): string {
        const startDate = dayjs(broadcastPeriod.start_date).format('YYYY年M月D日');
        const endDate = dayjs(broadcastPeriod.end_date).format('YYYY年M月D日');
        const dateRange = startDate === endDate ? startDate : `${startDate}〜${endDate}`;
        return `${broadcastPeriod.channel.name} · ${dateRange} · ${broadcastPeriod.recorded_programs.length}件`;
    }
}

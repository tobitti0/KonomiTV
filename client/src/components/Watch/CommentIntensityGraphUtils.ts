/**
 * コメント勢いグラフの描画サイズ
 *
 * SVG は実際の表示サイズに合わせて伸縮するため、この値はパスを組み立てるための論理座標としてのみ使う。
 * 横方向を十分細かくしておくことで、小数点以下の丸めによるグラフのガタつきを抑えている。
 */
export const COMMENT_INTENSITY_GRAPH_WIDTH = 1000;
export const COMMENT_INTENSITY_GRAPH_HEIGHT = 100;

/** グラフの上下に確保する余白 */
const GRAPH_VERTICAL_PADDING = 6;

/** 1つの録画番組で生成する集計区間数の上限目安 */
const TARGET_BUCKET_COUNT = 360;

/** 短い番組でも細かくなりすぎないようにする最小集計間隔 (秒) */
const MINIMUM_BUCKET_DURATION_SECONDS = 10;

/** 勢いの計算に必要なコメント時刻だけを表す最小インターフェース */
interface ICommentTiming {
    playback_position: number;
}

/** SVG に渡すグラフパス */
export interface ICommentIntensityGraphPaths {
    line_path: string;
    area_path: string;
}

/**
 * コメントの再生位置から、一定時間ごとのコメント勢いを計算する
 *
 * 集計点が多すぎると長時間番組や大量コメントで SVG 描画が重くなるため、番組の長さに応じて集計間隔を調整する。
 * 各区間の値には 1:2:1 の移動加重平均を適用し、単一コメントによる細かなギザつきだけを抑える。
 *
 * @param comments 過去ログコメントの再生位置
 * @param duration_seconds 録画ファイルの再生時間 (秒)
 * @returns 時系列順のコメント勢い
 */
export function calculateCommentIntensity(
    comments: readonly ICommentTiming[],
    duration_seconds: number,
): number[] {

    // コメントまたは有効な再生時間がなければ、グラフは描画しない
    if (comments.length === 0 || Number.isFinite(duration_seconds) === false || duration_seconds <= 0) {
        return [];
    }

    // 30分番組なら10秒、2時間番組なら20秒程度になるよう、最大約360区間に収める
    const bucket_duration_seconds = Math.max(
        MINIMUM_BUCKET_DURATION_SECONDS,
        Math.ceil(duration_seconds / TARGET_BUCKET_COUNT),
    );
    const bucket_count = Math.max(1, Math.ceil(duration_seconds / bucket_duration_seconds));
    const comment_counts = new Float64Array(bucket_count);
    let valid_comment_count = 0;

    // コメントを対応する再生区間へ1回の走査で振り分ける
    for (const comment of comments) {
        if (
            Number.isFinite(comment.playback_position) === false ||
            comment.playback_position < 0 ||
            comment.playback_position > duration_seconds
        ) {
            continue;
        }
        const bucket_index = Math.min(
            bucket_count - 1,
            Math.floor(comment.playback_position / bucket_duration_seconds),
        );
        comment_counts[bucket_index] += 1;
        valid_comment_count += 1;
    }

    // 配列にコメントがあっても再生時間外のデータしかなければ、グラフは描画しない
    if (valid_comment_count === 0) {
        return [];
    }

    // 前後の区間を含む 1:2:1 の移動加重平均で、勢いの大きな流れを残しつつノイズを抑える
    return Array.from(comment_counts, (count, index) => {
        const previous_count = index > 0 ? comment_counts[index - 1] : count;
        const next_count = index < comment_counts.length - 1 ? comment_counts[index + 1] : count;
        return (previous_count + count * 2 + next_count) / 4;
    });
}

/**
 * コメント勢いから、折れ線と塗りつぶし領域の SVG パスを生成する
 *
 * ごく一部の極端なピークで他の区間が潰れないよう、上位2%地点を表示上限にする。
 * パスは Catmull-Rom スプラインを3次ベジェ曲線へ変換し、Chart.js などの描画ライブラリを使わず滑らかに描画する。
 *
 * @param intensity_values 時系列順のコメント勢い
 * @returns 折れ線と塗りつぶし領域の SVG パス
 */
export function buildCommentIntensityGraphPaths(
    intensity_values: readonly number[],
): ICommentIntensityGraphPaths {

    if (intensity_values.length === 0) {
        return {
            line_path: '',
            area_path: '',
        };
    }

    // 外れ値に強い表示上限を求める。値がすべて0の場合でも0除算しないよう最低値を1にする
    const sorted_values = [...intensity_values].sort((left, right) => left - right);
    const percentile_index = Math.min(
        sorted_values.length - 1,
        Math.floor((sorted_values.length - 1) * 0.98),
    );
    const display_maximum = Math.max(1, sorted_values[percentile_index]);
    const graph_baseline = COMMENT_INTENSITY_GRAPH_HEIGHT - GRAPH_VERTICAL_PADDING;
    const graph_height = COMMENT_INTENSITY_GRAPH_HEIGHT - GRAPH_VERTICAL_PADDING * 2;

    // 集計値を SVG の論理座標へ変換する
    const points = intensity_values.map((value, index) => {
        const x = intensity_values.length === 1 ?
            COMMENT_INTENSITY_GRAPH_WIDTH / 2 :
            index / (intensity_values.length - 1) * COMMENT_INTENSITY_GRAPH_WIDTH;
        const normalized_value = Math.min(1, Math.max(0, value / display_maximum));
        const y = graph_baseline - normalized_value * graph_height;
        return {x, y};
    });

    // 1点だけの場合は横一線として描画する
    if (points.length === 1) {
        const line_path = `M 0 ${points[0].y} L ${COMMENT_INTENSITY_GRAPH_WIDTH} ${points[0].y}`;
        return {
            line_path,
            area_path: `${line_path} L ${COMMENT_INTENSITY_GRAPH_WIDTH} ${graph_baseline} L 0 ${graph_baseline} Z`,
        };
    }

    // Catmull-Rom スプラインを3次ベジェ曲線へ変換し、隣接する点を滑らかにつなぐ
    let line_path = `M ${points[0].x} ${points[0].y}`;
    for (let index = 0; index < points.length - 1; index++) {
        const previous_point = points[Math.max(0, index - 1)];
        const current_point = points[index];
        const next_point = points[index + 1];
        const following_point = points[Math.min(points.length - 1, index + 2)];
        const first_control_x = current_point.x + (next_point.x - previous_point.x) / 6;
        const first_control_y = current_point.y + (next_point.y - previous_point.y) / 6;
        const second_control_x = next_point.x - (following_point.x - current_point.x) / 6;
        const second_control_y = next_point.y - (following_point.y - current_point.y) / 6;
        line_path += ` C ${first_control_x} ${first_control_y}, ${second_control_x} ${second_control_y}, ${next_point.x} ${next_point.y}`;
    }

    return {
        line_path,
        area_path: `${line_path} L ${COMMENT_INTENSITY_GRAPH_WIDTH} ${graph_baseline} L 0 ${graph_baseline} Z`,
    };
}

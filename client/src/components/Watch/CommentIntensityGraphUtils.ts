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

/** コメント勢いグラフの集計・表示設定 */
export interface ICommentIntensityGraphSettings {
    /** 番組の長さにかかわらず目標とする集計点数 */
    readonly target_point_count: number;
    /** 短い番組で刻みが細かくなりすぎないようにする最小集計間隔 (秒) */
    readonly minimum_bucket_duration_seconds: number;
    /** 長い番組で山が潰れすぎないようにする最大集計間隔 (秒) */
    readonly maximum_bucket_duration_seconds: number;
    /** 前後何区間までを平滑化へ含めるか */
    readonly smoothing_radius: number;
    /** ガウシアン平滑化の標準偏差 (区間数) */
    readonly smoothing_standard_deviation: number;
    /** 番組内の通常水準を求める長期ガウシアン平滑化の半径 (区間数) */
    readonly baseline_smoothing_radius: number;
    /** 番組内の通常水準を求める長期ガウシアン平滑化の標準偏差 (区間数) */
    readonly baseline_smoothing_standard_deviation: number;
    /** 通常水準を上回った局所ピークの強調度 */
    readonly relative_peak_amplification: number;
    /** 通常水準に対する局所ピーク上昇率の上限 */
    readonly maximum_relative_peak_lift: number;
    /** 表示上限に採用するパーセンタイル (0〜1) */
    readonly display_maximum_percentile: number;
    /** コメント数が多い番組でもピークを見分けやすくする表示コントラスト */
    readonly contrast_exponent: number;
}

/**
 * コメント勢いグラフの既定設定
 *
 * 将来ユーザー設定へ移行しやすいよう、集計方法と見た目に関わる値を一箇所へまとめている。
 */
export const DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS: Readonly<ICommentIntensityGraphSettings> = Object.freeze({
    target_point_count: 360,
    minimum_bucket_duration_seconds: 5,
    maximum_bucket_duration_seconds: 30,
    smoothing_radius: 3,
    smoothing_standard_deviation: 1.5,
    baseline_smoothing_radius: 12,
    baseline_smoothing_standard_deviation: 6,
    relative_peak_amplification: 0.25,
    maximum_relative_peak_lift: 2,
    display_maximum_percentile: 0.98,
    contrast_exponent: 1.5,
});

/** 勢いの計算に必要なコメント時刻だけを表す最小インターフェース */
interface ICommentTiming {
    playback_position: number;
}

/** SVG に渡すグラフパス */
export interface ICommentIntensityGraphPaths {
    line_path: string;
    area_path: string;
}

/** コメント勢いグラフと同じコメントデータから求める統計情報 */
export interface ICommentIntensityStatistics {
    total_comments: number;
    max_comments_per_minute: number;
    average_comments_per_minute: number;
}

/** コメント未取得時に表示する空の統計情報 */
export const EMPTY_COMMENT_INTENSITY_STATISTICS: Readonly<ICommentIntensityStatistics> = Object.freeze({
    total_comments: 0,
    max_comments_per_minute: 0,
    average_comments_per_minute: 0,
});

/** グラフパスを構成する論理座標 */
interface IGraphPoint {
    x: number;
    y: number;
}

/**
 * 配列へガウシアン平滑化を適用する
 *
 * グラフ両端では範囲内の値だけを使って重みを再正規化し、先頭・末尾の値が過剰に強調されるのを防ぐ。
 */
function applyGaussianSmoothing(
    values: ArrayLike<number>,
    radius: number,
    standard_deviation: number,
): number[] {

    const normalized_radius = Math.max(0, Math.round(radius));
    const normalized_standard_deviation = Math.max(0.1, standard_deviation);
    const gaussian_weights = Array.from({length: normalized_radius * 2 + 1}, (_, index) => {
        const offset = index - normalized_radius;
        return Math.exp(-(offset ** 2) / (2 * normalized_standard_deviation ** 2));
    });

    return Array.from({length: values.length}, (_, index) => {
        let weighted_value = 0;
        let total_weight = 0;
        for (let offset = -normalized_radius; offset <= normalized_radius; offset++) {
            const source_index = index + offset;
            if (source_index < 0 || source_index >= values.length) {
                continue;
            }
            const weight = gaussian_weights[offset + normalized_radius];
            weighted_value += values[source_index] * weight;
            total_weight += weight;
        }
        return total_weight > 0 ? weighted_value / total_weight : values[index];
    });
}

/**
 * 点列を値の範囲から飛び出さない単調3次曲線で接続し、SVG パスを生成する
 *
 * Fritsch-Carlson 法で接線を制限し、通常の Catmull-Rom 補間で発生しうる架空の山や谷を防ぐ。
 */
function buildMonotoneCurvePath(points: readonly IGraphPoint[]): string {

    const segment_slopes = Array.from({length: points.length - 1}, (_, index) => {
        return (points[index + 1].y - points[index].y) / (points[index + 1].x - points[index].x);
    });
    const tangents = new Array<number>(points.length);
    tangents[0] = segment_slopes[0];
    tangents[points.length - 1] = segment_slopes[segment_slopes.length - 1];

    // 符号が変わる極値では接線を水平にし、それ以外では前後区間の平均傾斜を使う
    for (let index = 1; index < points.length - 1; index++) {
        const previous_slope = segment_slopes[index - 1];
        const next_slope = segment_slopes[index];
        tangents[index] = previous_slope * next_slope <= 0 ? 0 : (previous_slope + next_slope) / 2;
    }

    // 各区間の傾斜に対して接線が強すぎる場合は縮小し、オーバーシュートを防ぐ
    for (let index = 0; index < segment_slopes.length; index++) {
        const segment_slope = segment_slopes[index];
        if (segment_slope === 0) {
            tangents[index] = 0;
            tangents[index + 1] = 0;
            continue;
        }
        const first_ratio = tangents[index] / segment_slope;
        const second_ratio = tangents[index + 1] / segment_slope;
        const ratio_length = Math.hypot(first_ratio, second_ratio);
        if (ratio_length > 3) {
            const scale = 3 / ratio_length;
            tangents[index] = scale * first_ratio * segment_slope;
            tangents[index + 1] = scale * second_ratio * segment_slope;
        }
    }

    let path = `M ${points[0].x} ${points[0].y}`;
    for (let index = 0; index < points.length - 1; index++) {
        const current_point = points[index];
        const next_point = points[index + 1];
        const segment_width = next_point.x - current_point.x;
        const first_control_x = current_point.x + segment_width / 3;
        const first_control_y = current_point.y + tangents[index] * segment_width / 3;
        const second_control_x = next_point.x - segment_width / 3;
        const second_control_y = next_point.y - tangents[index + 1] * segment_width / 3;
        path += ` C ${first_control_x} ${first_control_y}, ${second_control_x} ${second_control_y}, ${next_point.x} ${next_point.y}`;
    }
    return path;
}

/**
 * コメントの再生位置から、一定時間ごとのコメント勢いを計算する
 *
 * 番組の長さを目標点数で割って集計間隔を決め、短い番組と長い番組で画面上の山の密度を揃える。
 * 各区間の値にはガウシアン平滑化を適用し、広い範囲の盛り上がりを残しながら細かなギザつきを抑える。
 *
 * @param comments 過去ログコメントの再生位置
 * @param duration_seconds 録画ファイルの再生時間 (秒)
 * @param settings コメント勢いグラフの集計・表示設定
 * @returns 時系列順のコメント勢い
 */
export function calculateCommentIntensity(
    comments: readonly ICommentTiming[],
    duration_seconds: number,
    settings: Readonly<ICommentIntensityGraphSettings> = DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS,
): number[] {

    // コメントまたは有効な再生時間がなければ、グラフは描画しない
    if (comments.length === 0 || Number.isFinite(duration_seconds) === false || duration_seconds <= 0) {
        return [];
    }

    // 30分番組なら5秒、2時間番組なら20秒程度になるよう、目標約360区間に揃える
    const target_point_count = Math.max(1, Math.round(settings.target_point_count));
    const minimum_bucket_duration_seconds = Math.max(1, settings.minimum_bucket_duration_seconds);
    const maximum_bucket_duration_seconds = Math.max(
        minimum_bucket_duration_seconds,
        settings.maximum_bucket_duration_seconds,
    );
    const bucket_duration_seconds = Math.min(
        maximum_bucket_duration_seconds,
        Math.max(
            minimum_bucket_duration_seconds,
            Math.ceil(duration_seconds / target_point_count),
        ),
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

    // 短期勢いは細かなノイズだけを抑え、コメントが集中した区間の輪郭を残す
    const short_term_intensity = applyGaussianSmoothing(
        comment_counts,
        settings.smoothing_radius,
        settings.smoothing_standard_deviation,
    );

    // 長期基準は番組内のその周辺における「通常のコメント量」を表す
    const baseline_intensity = applyGaussianSmoothing(
        short_term_intensity,
        settings.baseline_smoothing_radius,
        settings.baseline_smoothing_standard_deviation,
    );

    // コメントの絶対量を主体にしつつ、長期基準を上回った局所的な盛り上がりだけを追加で強調する
    const relative_peak_amplification = Math.max(0, settings.relative_peak_amplification);
    const maximum_relative_peak_lift = Math.max(0, settings.maximum_relative_peak_lift);
    return short_term_intensity.map((value, index) => {
        const baseline = Math.max(1, baseline_intensity[index]);
        const relative_peak_lift = Math.min(
            maximum_relative_peak_lift,
            Math.max(0, (value - baseline) / baseline),
        );
        return value * (1 + relative_peak_lift * relative_peak_amplification);
    });
}

/**
 * コメント時刻から、総コメント数・最大コメント毎分・平均コメント毎分を求める
 *
 * 最大コメント毎分は固定された「何分台」ではなく、任意の連続60秒間に含まれる最大件数として計算する。
 */
export function calculateCommentIntensityStatistics(
    comments: readonly ICommentTiming[],
    duration_seconds: number,
): ICommentIntensityStatistics {

    if (comments.length === 0 || Number.isFinite(duration_seconds) === false || duration_seconds <= 0) {
        return {...EMPTY_COMMENT_INTENSITY_STATISTICS};
    }

    const playback_positions = comments
        .map((comment) => comment.playback_position)
        .filter((position) => Number.isFinite(position) && position >= 0 && position <= duration_seconds)
        .sort((left, right) => left - right);
    if (playback_positions.length === 0) {
        return {...EMPTY_COMMENT_INTENSITY_STATISTICS};
    }

    let window_start_index = 0;
    let max_comments_per_minute = 0;
    for (let window_end_index = 0; window_end_index < playback_positions.length; window_end_index++) {
        while (
            playback_positions[window_end_index] - playback_positions[window_start_index] >= 60 &&
            window_start_index < window_end_index
        ) {
            window_start_index += 1;
        }
        max_comments_per_minute = Math.max(
            max_comments_per_minute,
            window_end_index - window_start_index + 1,
        );
    }

    return {
        // 総数と平均はプレイヤーへ渡されたコメント件数と一致させ、録画末尾をわずかに越えたコメントも含める
        total_comments: comments.length,
        max_comments_per_minute,
        average_comments_per_minute: comments.length / duration_seconds * 60,
    };
}

/**
 * コメント勢いから、折れ線と塗りつぶし領域の SVG パスを生成する
 *
 * ごく一部の極端なピークで他の区間が潰れないよう、上位2%地点を表示上限にする。
 * さらに緩やかなコントラスト補正を加え、全編を通してコメントが多い番組でもピークを見分けやすくする。
 * パスは単調3次曲線をベジェ曲線で表現し、Chart.js などの描画ライブラリを使わず滑らかに描画する。
 *
 * @param intensity_values 時系列順のコメント勢い
 * @param settings コメント勢いグラフの集計・表示設定
 * @returns 折れ線と塗りつぶし領域の SVG パス
 */
export function buildCommentIntensityGraphPaths(
    intensity_values: readonly number[],
    settings: Readonly<ICommentIntensityGraphSettings> = DEFAULT_COMMENT_INTENSITY_GRAPH_SETTINGS,
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
        Math.floor(
            (sorted_values.length - 1) *
            Math.min(1, Math.max(0, settings.display_maximum_percentile)),
        ),
    );
    const display_maximum = Math.max(1, sorted_values[percentile_index]);
    const graph_baseline = COMMENT_INTENSITY_GRAPH_HEIGHT - GRAPH_VERTICAL_PADDING;
    const graph_height = COMMENT_INTENSITY_GRAPH_HEIGHT - GRAPH_VERTICAL_PADDING * 2;

    // 集計値を SVG の論理座標へ変換する
    const points = intensity_values.map((value, index) => {
        const x = intensity_values.length === 1 ?
            COMMENT_INTENSITY_GRAPH_WIDTH / 2 :
            index / (intensity_values.length - 1) * COMMENT_INTENSITY_GRAPH_WIDTH;
        const normalized_value = Math.pow(
            Math.min(1, Math.max(0, value / display_maximum)),
            Math.max(0.01, settings.contrast_exponent),
        );
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

    // 値の範囲から飛び出さない単調3次曲線で、隣接する点を滑らかにつなぐ
    const line_path = buildMonotoneCurvePath(points);

    return {
        line_path,
        area_path: `${line_path} L ${COMMENT_INTENSITY_GRAPH_WIDTH} ${graph_baseline} L 0 ${graph_baseline} Z`,
    };
}

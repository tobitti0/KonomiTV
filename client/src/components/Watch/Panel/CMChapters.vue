<template>
    <section class="cm-chapters">
        <div class="cm-chapters__header">
            <h2 class="cm-chapters__heading">チャプター</h2>
            <div class="cm-chapters__actions">
                <span class="cm-chapters__hint" v-if="cmSections !== null">選択するとその位置へ移動します</span>
                <button v-ripple class="cm-chapters__reanalyze"
                    :disabled="isAnalysisRequesting || cmAnalysisStatus === 'Analyzing'"
                    v-if="userStore.user?.is_admin &&
                        playerStore.recorded_program.recorded_video.status === 'Recorded' &&
                        playerStore.recorded_program.recorded_video.container_format === 'MPEG-TS'"
                    @click="startCMAnalysis()">
                    <Icon icon="fluent:arrow-sync-16-regular" height="15px" />
                    <span>{{cmSections !== null ? '再解析' : '解析'}}</span>
                </button>
            </div>
        </div>
        <div class="cm-chapters__status cm-chapters__status--error" v-if="cmAnalysisStatus === 'Failed'">
            {{cmAnalysisErrorMessage}}
            <span v-if="cmSections !== null">以前の正常な解析結果を表示しています。</span>
        </div>
        <div class="cm-chapters__status" v-if="cmAnalysisStatus === 'Analyzing'">
            CM 区間を解析中です…
        </div>
        <div class="cm-chapters__status" v-else-if="cmAnalysisStatus !== 'Failed' && cmSections === null">
            CM 区間は未解析です
        </div>
        <div class="cm-chapters__list" v-else-if="cmSections !== null">
            <button v-ripple class="cm-chapters__item"
                :class="{'cm-chapters__item--active': isActiveChapter(chapter)}"
                :aria-current="isActiveChapter(chapter) ? 'true' : undefined"
                :key="`${chapter.kind}-${chapter.startTime}`"
                v-for="chapter in chapters"
                @click="seekToChapter(chapter)">
                <span class="cm-chapters__kind"
                    :class="{'cm-chapters__kind--commercial': chapter.kind === 'CM'}">
                    {{chapter.label}}
                </span>
                <span class="cm-chapters__time">
                    {{formatTime(chapter.startTime)}}–{{formatTime(chapter.endTime)}}
                </span>
                <Icon class="cm-chapters__seek-icon" icon="fluent:play-16-filled" height="17px" />
            </button>
        </div>
    </section>
</template>
<script setup lang="ts">

import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import type { PlayerEvents } from '@/stores/PlayerStore';

import Message from '@/message';
import Maintenance from '@/services/Maintenance';
import Videos from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';

type ChapterKind = 'CM' | 'Main';

interface Chapter {
    kind: ChapterKind;
    label: string;
    startTime: number;
    endTime: number;
}

const playerStore = usePlayerStore();
const userStore = useUserStore();
const playbackPosition = ref(0);
const isAnalysisRequesting = ref(false);
const isComponentActive = ref(true);

const cmAnalysisStatus = computed(() => playerStore.recorded_program.recorded_video.cm_analysis_status);
const cmSections = computed(() => playerStore.recorded_program.recorded_video.cm_sections);
const cmAnalysisErrorMessage = computed(() => {
    const error = playerStore.recorded_program.recorded_video.cm_analysis_error;
    if (error === null) {
        return 'CM 区間解析に失敗しました。';
    }
    const stageNames: Record<typeof error.stage, string> = {
        Preparation: '解析準備',
        ChapterFile: 'チャプターファイル読込',
        ChapterEXE: 'chapter_exe',
        LogoFrame: 'logoframe',
        JoinLogoSCP: 'join_logo_scp',
        DTVIndex: 'dtvindex',
        Canceled: 'キャンセル処理',
        Unknown: '不明な工程',
    };
    const exitCode = error.exit_code !== null ? `（終了コード: ${error.exit_code}）` : '';
    return `${stageNames[error.stage]}で CM 区間解析に失敗しました${exitCode}。`;
});

// API の CM 区間から、本編を含む動画全体の連続したチャプターを組み立てる
const chapters = computed<Chapter[]>(() => {
    if (cmSections.value === null) {
        return [];
    }

    const duration = Math.max(0, playerStore.recorded_program.recorded_video.duration);
    const sections = [...cmSections.value]
        .map(section => ({
            startTime: Math.max(0, Math.min(duration, section.start_time)),
            endTime: Math.max(0, Math.min(duration, section.end_time)),
        }))
        .filter(section => section.endTime > section.startTime)
        .sort((left, right) => left.startTime - right.startTime);

    const result: Chapter[] = [];
    let cursor = 0;
    let mainIndex = 1;
    let commercialIndex = 1;
    for (const section of sections) {
        const commercialStart = Math.max(cursor, section.startTime);
        if (commercialStart > cursor) {
            result.push({
                kind: 'Main',
                label: `本編 ${mainIndex++}`,
                startTime: cursor,
                endTime: commercialStart,
            });
        }
        if (section.endTime > cursor) {
            result.push({
                kind: 'CM',
                label: `CM ${commercialIndex++}`,
                startTime: commercialStart,
                endTime: section.endTime,
            });
            cursor = section.endTime;
        }
    }
    if (cursor < duration || result.length === 0) {
        result.push({
            kind: 'Main',
            label: `本編 ${mainIndex}`,
            startTime: cursor,
            endTime: duration,
        });
    }
    return result;
});

const onPlaybackPositionChanged = (event: PlayerEvents['PlaybackPositionChanged']): void => {
    playbackPosition.value = event.playback_position;
};

const isActiveChapter = (chapter: Chapter): boolean => {
    const lastChapter = chapters.value[chapters.value.length - 1];
    return playbackPosition.value >= chapter.startTime &&
        (playbackPosition.value < chapter.endTime ||
            (chapter === lastChapter && playbackPosition.value === chapter.endTime));
};

const seekToChapter = (chapter: Chapter): void => {
    playerStore.event_emitter.emit('SeekRequest', {
        playback_position: chapter.startTime,
    });
};

const formatTime = (time: number): string => {
    const totalSeconds = Math.max(0, Math.floor(time));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return [hours, minutes, seconds].map(value => value.toString().padStart(2, '0')).join(':');
};

const startCMAnalysis = async (): Promise<void> => {
    if (isAnalysisRequesting.value === true || cmAnalysisStatus.value === 'Analyzing') {
        return;
    }
    isAnalysisRequesting.value = true;
    const videoID = playerStore.recorded_program.id;
    const startedJob = await Videos.startCMAnalysis(videoID);
    if (startedJob === null) {
        isAnalysisRequesting.value = false;
        return;
    }

    Message.info('CM 区間解析を開始しました。解析中も動画を再生できます。');
    try {
        // API リクエストとは独立したバックグラウンドジョブなので、完了まで短い間隔で状態だけを取得する
        while (isComponentActive.value === true) {
            await Utils.sleep(2.0);
            const job = await Maintenance.fetchCMAnalysisStatus();
            if (job === null || ['Completed', 'Canceled', 'Failed'].includes(job.status)) {
                break;
            }
        }

        // 解析完了後の CM 区間とエラー状態をプレイヤーパネルへ反映する
        if (isComponentActive.value === true) {
            const recordedProgram = await Videos.fetchVideo(videoID);
            if (recordedProgram !== null) {
                playerStore.recorded_program = recordedProgram;
                if (recordedProgram.recorded_video.cm_analysis_status === 'Completed') {
                    Message.success('CM 区間解析が完了しました。');
                } else if (recordedProgram.recorded_video.cm_analysis_status === 'Failed') {
                    Message.error('CM 区間解析に失敗しました。詳細はチャプターパネルで確認できます。');
                }
            }
        }
    } finally {
        isAnalysisRequesting.value = false;
    }
};

onMounted(() => {
    playerStore.event_emitter.on('PlaybackPositionChanged', onPlaybackPositionChanged);
});

onBeforeUnmount(() => {
    isComponentActive.value = false;
    playerStore.event_emitter.off('PlaybackPositionChanged', onPlaybackPositionChanged);
});

</script>
<style lang="scss" scoped>

.cm-chapters {
    margin-top: 24px;

    &__header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
    }

    &__heading {
        font-size: 18px;
        @include smartphone-horizontal {
            font-size: 16px;
        }
    }

    &__actions {
        display: flex;
        align-items: center;
        gap: 8px;
    }

    &__reanalyze {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        padding: 4px 7px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11px;
        border-radius: 4px;

        &:hover:not(:disabled) {
            color: rgb(var(--v-theme-primary-lighten-1));
        }

        &:disabled {
            opacity: 0.5;
        }
    }

    &__hint,
    &__status {
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11px;
    }

    &__status {
        padding: 14px 0;

        &--error {
            display: flex;
            flex-direction: column;
            gap: 3px;
            color: rgb(var(--v-theme-error-lighten-1));
        }
    }

    &__list {
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin-top: 10px;
    }

    &__item {
        display: grid;
        grid-template-columns: minmax(72px, auto) 1fr auto;
        align-items: center;
        gap: 10px;
        width: 100%;
        padding: 9px 10px;
        color: rgb(var(--v-theme-text-darken-1));
        text-align: left;
        background: rgb(var(--v-theme-background-lighten-1));
        border: 1px solid transparent;
        border-radius: 5px;
        transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
        cursor: pointer;

        &:hover,
        &--active {
            color: rgb(var(--v-theme-text));
            border-color: rgba(var(--v-theme-primary), 0.7);
        }

        &--active {
            background: rgba(var(--v-theme-primary), 0.12);
        }
    }

    &__kind {
        font-size: 12.5px;
        font-weight: 700;

        &--commercial {
            color: rgb(var(--v-theme-primary-lighten-1));
        }
    }

    &__time {
        font-size: 11.5px;
        font-variant-numeric: tabular-nums;
    }

    &__seek-icon {
        flex-shrink: 0;
    }
}

</style>

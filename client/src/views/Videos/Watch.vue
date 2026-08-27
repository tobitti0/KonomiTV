<template>
    <Watch :playback_mode="'Video'" />
</template>
<script lang="ts">

import { mapStores } from 'pinia';
import { defineComponent } from 'vue';

import Watch from '@/components/Watch/Watch.vue';
import Message from '@/message';
import OfflineVideos from '@/services/OfflineVideos';
import PlayerController from '@/services/player/PlayerController';
import SeriesService from '@/services/Series';
import Videos from '@/services/Videos';
import usePlayerStore from '@/stores/PlayerStore';
import useSettingsStore from '@/stores/SettingsStore';
import { SeriesUtils } from '@/utils';

// PlayerController のインスタンス
// data() 内に記述すると再帰的にリアクティブ化され重くなる上リアクティブにする必要自体がないので、グローバル変数にしている
let player_controller: PlayerController | null = null;

export default defineComponent({
    name: 'Video-Watch',
    components: {
        Watch,
    },
    computed: {
        ...mapStores(usePlayerStore, useSettingsStore),
    },
    data() {
        return {
            // ended イベントが重複して発生しても次話への遷移を一度だけに制限する
            is_navigating_to_next_program: false,
        };
    },
    // 開始時に実行
    created() {

        // 下記以外の視聴画面の開始処理は Watch コンポーネントの方で自動的に行われる

        // PlayerController から再生終了通知を受け取り、同じ放送期間の次話へ移動する
        this.playerStore.event_emitter.on('PlaybackEnded', this.playNextProgram);

        // 再生セッションを初期化
        this.init();
    },
    // チャンネル切り替え時に実行
    // コンポーネント（インスタンス）は再利用される
    // ref: https://v3.router.vuejs.org/ja/guide/advanced/navigation-guards.html#%E3%83%AB%E3%83%BC%E3%83%88%E5%8D%98%E4%BD%8D%E3%82%AB%E3%82%99%E3%83%BC%E3%83%88%E3%82%99
    beforeRouteUpdate(to, from, next) {

        // 前の再生セッションを破棄して終了し、完了を待ってから再度初期化する
        const destroy_promise = this.destroy();
        destroy_promise.then(() => this.init());

        // 次のルートに置き換え
        next();
    },
    // 終了前に実行
    beforeUnmount() {

        // この画面が破棄された後に再生終了イベントを受け取らないよう解除する
        this.playerStore.event_emitter.off('PlaybackEnded', this.playNextProgram);

        // destroy() を実行
        // 別のページへ遷移するため、DPlayer のインスタンスを確実に破棄する
        // さもなければ、ブラウザがリロードされるまでバックグラウンドで永遠に再生され続けてしまう
        this.destroy();

        // 上記以外の視聴画面の終了処理は Watch コンポーネントの方で自動的に行われる
    },
    methods: {

        // 再生セッションを初期化する
        async init() {

            // URL 上の録画番組 ID が未定義なら実行しない (フェイルセーフ)
            // 基本あり得ないはずだが、念のため
            if (this.$route.params.video_id === undefined) {
                this.$router.push({path: '/not-found/'});
                return;
            }

            const video_id_text = this.$route.params.video_id;
            if (typeof video_id_text !== 'string' || /^[1-9]\d*$/.test(video_id_text) === false) {
                this.$router.push({path: '/not-found/'});
                return;
            }
            const video_id = Number(video_id_text);

            // オフライン保存ページから開いた場合は保存時点の番組情報を使い、通常再生の通信失敗時だけ保存版へ切り替える
            let offline_video = this.$route.query.source === 'offline' ? await OfflineVideos.getVideo(video_id) : null;
            let recorded_program = offline_video?.program ?? await Videos.fetchVideo(video_id);
            if (recorded_program === null && offline_video === null) {
                offline_video = await OfflineVideos.getVideo(video_id);
                recorded_program = offline_video?.program ?? null;
            }
            if (recorded_program === null) {
                this.$router.push({path: '/not-found/'});
                return;
            }
            this.playerStore.recorded_program = recorded_program;
            this.playerStore.series = null;
            // オフライン再生ではネットワークへ依存せず再生を開始できることを優先し、シリーズ API は呼び出さない
            // 通常再生、または通信失敗後にオフライン保存へフォールバックした場合だけシリーズ情報を取得する
            const series_id = offline_video === null ? recorded_program.series_id : null;
            this.playerStore.is_series_loading = series_id !== null;
            this.is_navigating_to_next_program = false;

            // シリーズ情報はプレイヤー初期化と並行取得し、右パネルと次話判定で共有する
            // ルート切り替え中に前のリクエストが遅れて完了しても、現在の録画へ誤ったシリーズを設定しない
            const initializing_video_id = recorded_program.id;
            const series_fetch_promise = (async () => {
                const series = series_id !== null
                    ? await SeriesService.fetchSeries(series_id)
                    : null;
                if (this.playerStore.recorded_program.id === initializing_video_id) {
                    this.playerStore.series = series;
                    this.playerStore.is_series_loading = false;
                }
            })();
            this.playerStore.is_offline_playback = offline_video !== null;
            this.playerStore.offline_video = offline_video;

            // PlayerController を初期化
            player_controller = new PlayerController('Video');
            await Promise.all([
                player_controller.init(),
                series_fetch_promise,
            ]);
        },

        // 同じ放送期間内で現在の番組より後に放送された、次の再生可能な録画番組へ移動する
        playNextProgram(): void {
            if (this.is_navigating_to_next_program === true || this.playerStore.series === null) {
                return;
            }
            const nextProgram = SeriesUtils.getNextProgram(
                this.playerStore.series,
                this.playerStore.recorded_program.id,
            );
            if (nextProgram === null) {
                return;
            }

            this.is_navigating_to_next_program = true;
            Message.info(`次のエピソード「${nextProgram.title}」を再生します。`);
            this.$router.push(`/videos/watch/${nextProgram.id}`);
        },

        // 再生セッションを破棄する
        // 再生する録画番組を切り替える際にも実行される
        async destroy() {

            // PlayerController を破棄
            if (player_controller !== null) {
                await player_controller.destroy();
                player_controller = null;
            }
        }
    }
});

</script>

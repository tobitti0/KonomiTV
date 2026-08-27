
/**
 * services/ 直下の各クラスは、KonomiTV サーバーへの API リクエストを抽象化し、
 * API レスポンスの受け取りと、エラーが発生した際のエラーハンドリング (エラーメッセージ表示) までを責務として負う
 */

import axios, { AxiosError, AxiosRequestConfig, AxiosResponse, AxiosResponseHeaders, RawAxiosResponseHeaders } from 'axios';

import Message from '@/message';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';


/** API リクエスト成功時のレスポンスを表すインターフェイス */
export interface ISuccessResponse<T> {
    type: 'success';
    status: number;
    headers: RawAxiosResponseHeaders | AxiosResponseHeaders;
    data: T;
}

/** API リクエスト失敗時のレスポンスを表すインターフェイス */
export interface IErrorResponse {
    type: 'error';
    status: number;
    headers: RawAxiosResponseHeaders | AxiosResponseHeaders;
    data: IErrorResponseData;
    error: AxiosError<IErrorResponseData>;
}

/**
 * API リクエスト失敗時にサーバーから返されるエラーレスポンスを表すインターフェイス
 * HTTP リクエスト自体が失敗した場合は、detail に AxiosError のエラーメッセージが入る
 */
export interface IErrorResponseData {
    detail: string | [{
        type: string;
        loc: (string | number)[];
        msg: string;
        input?: any;
        url?: string;
    }]
}


/**
 * services/ 以下の各クラスから呼び出される、Axios の薄いラッパー
 * エラーハンドリングを容易にするために、レスポンスを ISuccessResponse と IErrorResponse に分けて返す
 * ref: https://zenn.dev/engineer_titan/articles/291c9fccb338e2
 */
class APIClient {

    // Cloudflare Access の再認証へ遷移中かどうか
    // 複数の API リクエストが同時に 401 を受信しても、Service Worker の解除と再読み込みを一度だけ実行する
    private static is_cloudflare_access_reloading = false;


    /**
     * Cloudflare Access の認証期限切れで返された 401 レスポンスかどうかを判定する
     * @param response Axios のエラーレスポンス
     * @returns Cloudflare Access の認証期限切れと判断できる場合は true
     */
    private static isCloudflareAccessUnauthorized(response: AxiosResponse): boolean {

        // KonomiTV 自身も未ログイン時に 401 と JSON の detail を返すため、
        // 401 かつ KonomiTV 標準のエラーレスポンス形式ではない場合だけ Cloudflare Access と判断する
        if (response.status !== 401) {
            return false;
        }
        const response_data: unknown = response.data;
        return (
            typeof response_data !== 'object' ||
            response_data === null ||
            Object.prototype.hasOwnProperty.call(response_data, 'detail') === false
        );
    }


    /**
     * 現在のページを Cloudflare Access の再認証フローへ遷移させる
     * @returns 処理の完了を表す Promise
     */
    private static async reloadForCloudflareAccessAuthentication(): Promise<void> {

        // 同時に複数の API が失敗しても再読み込み処理は一度だけ実行する
        if (APIClient.is_cloudflare_access_reloading === true) {
            return;
        }
        APIClient.is_cloudflare_access_reloading = true;
        console.warn('[APIClient] Cloudflare Access session expired. Reloading the page for authentication...');

        try {
            // KonomiTV は PWA のため、通常の再読み込みでは Service Worker がキャッシュ済みの index.html を返し、
            // Cloudflare Access へトップレベルのリクエストが届かない可能性がある
            // 現在のページを制御する Service Worker だけを一度解除し、再読み込みを必ずネットワーク経由にする
            if ('serviceWorker' in navigator) {
                const registration = await navigator.serviceWorker.getRegistration();
                await registration?.unregister();
            }
        } catch (error) {
            // Service Worker の解除に失敗しても、通常の再読み込みで再認証できる可能性があるため処理を継続する
            console.warn('[APIClient] Failed to unregister the service worker before authentication:', error);
        }

        // トップレベルのページ遷移には X-Requested-With が付かないため、
        // Cloudflare Access が通常のログイン画面へリダイレクトし、認証後は現在の URL に戻る
        window.location.reload();
    }


    /**
     * Axios で HTTP リクエストを送信し、レスポンスを受け取る
     * @param request AxiosRequestConfig
     * @returns 成功なら ISuccessResponse 、失敗なら IErrorResponse を返す
     */
    static async request<T>(request: AxiosRequestConfig): Promise<ISuccessResponse<T> | IErrorResponse> {

        // API のベース URL を設定 (config.baseURL が指定されていない場合のみ)
        if (request.baseURL === undefined) {
            request.baseURL = Utils.api_base_url;
        }

        // リクエストヘッダーが指定されていない場合は空のオブジェクトを設定
        if (request.headers === undefined) {
            request.headers = {};
        }

        // 外部サイトへの HTTP/HTTPS リクエストでは実行しない
        // Utils.api_base_url を明示した絶対 URL も KonomiTV API として扱う
        const is_konomitv_request = (
            request.url?.startsWith('http') === false ||
            request.url?.startsWith(Utils.api_base_url) === true
        );
        if (is_konomitv_request === true) {

            // アクセストークンが取得できたら (=ログインされていれば)
            // 取得したアクセストークンを Authorization ヘッダーに Bearer トークンとしてセット
            // これを忘れると当然ながらログインしていない扱いになる
            const access_token = Utils.getAccessToken();
            if (access_token !== null) {
                request.headers['Authorization'] = `Bearer ${access_token}`;
            }

            // KonomiTV クライアントのバージョンを設定
            // 今のところ使わないが、将来的にクライアントとサーバーを分離することを見据えて念のため
            request.headers['X-KonomiTV-Version'] = Utils.version;

            // Cloudflare Access に AJAX リクエストであることを伝える
            // 認証期限切れ時にログインページへのリダイレクトではなく 401 を返させ、下記で再認証へ遷移できるようにする
            request.headers['X-Requested-With'] = 'XMLHttpRequest';
        }

        // リクエストのタイムアウト時間を30秒に設定
        // 既にタイムアウト時間が設定されている場合は上書きしない
        if (request.timeout === undefined) {
            request.timeout = 30 * 1000;
        }

        // リクエストのタイムアウト時に、一般的な ECONNABORTED の代わりに ETIMEDOUT を送出する
        request.transitional = {
            clarifyTimeoutError: true,
        };

        // Axios で HTTP リクエストを送信し、レスポンスを受け取る
        const result: AxiosResponse<T> | AxiosError<IErrorResponseData> = await axios.request(request).catch((error) => error);

        // エラーが発生した場合は IErrorResponse を返す
        if (result instanceof AxiosError) {
            console.error(result);

            // エラーレスポンスがあれば、エラー内容と AxiosError を IErrorResponse に入れて返す
            if (result.response) {
                // Cloudflare Access の認証期限切れを検出した場合、現在のページを再読み込みして再認証へ遷移する
                // KonomiTV 自身の 401 JSON は isCloudflareAccessUnauthorized() で除外され、従来のログアウト処理へ進む
                if (
                    is_konomitv_request === true &&
                    APIClient.isCloudflareAccessUnauthorized(result.response) === true
                ) {
                    void APIClient.reloadForCloudflareAccessAuthentication();
                }
                return {
                    type: 'error',
                    status: result.response.status,
                    headers: result.response.headers,
                    data: result.response.data,  // data には IErrorResponseData が入る
                    error: result,  // AxiosError をそのまま入れる
                };

            // エラーレスポンスがない場合は、AxiosError のみを IErrorResponse に入れて返す
            } else {
                return {
                    type: 'error',
                    status: NaN,  // ステータスコードは取得できないので NaN にする
                    headers: {},  // ヘッダーも取得できないので空のオブジェクトにする
                    data: {detail: result.message},  // data.detail に AxiosError のエラーメッセージを入れる
                    error: result,  // AxiosError をそのまま入れる
                };
            }

        // 正常にレスポンスが返ってきた場合は ISuccessResponse を返す
        } else {
            return {
                type: 'success',
                headers: result.headers,
                status: result.status,
                data: result.data,
            };
        }
    }


    /**
     * GET リクエストを送信する
     * @param url リクエスト先の URL
     * @param config AxiosRequestConfig
     * @returns 成功なら ISuccessResponse 、失敗なら IErrorResponse を返す
     */
    static async get<T = any, D = any>(url: string, config?: AxiosRequestConfig<D>): Promise<ISuccessResponse<T> | IErrorResponse> {
        const request: AxiosRequestConfig = {
            url: url,
            method: 'GET',
            ...config,
        };
        return await APIClient.request<T>(request);
    }


    /**
     * POST リクエストを送信する
     * @param url リクエスト先の URL
     * @param data 送信するデータ
     * @param config AxiosRequestConfig
     * @returns 成功なら ISuccessResponse 、失敗なら IErrorResponse を返す
     */
    static async post<T = any, D = any>(url: string, data?: D, config?: AxiosRequestConfig<D>): Promise<ISuccessResponse<T> | IErrorResponse> {
        const request: AxiosRequestConfig = {
            url: url,
            method: 'POST',
            data: data,
            ...config,
        };
        return await APIClient.request<T>(request);
    }


    /**
     * PUT リクエストを送信する
     * @param url リクエスト先の URL
     * @param data 送信するデータ
     * @param config AxiosRequestConfig
     * @returns 成功なら ISuccessResponse 、失敗なら IErrorResponse を返す
     */
    static async put<T = any, D = any>(url: string, data?: D, config?: AxiosRequestConfig<D>): Promise<ISuccessResponse<T> | IErrorResponse> {
        const request: AxiosRequestConfig = {
            url: url,
            method: 'PUT',
            data: data,
            ...config,
        };
        return await APIClient.request<T>(request);
    }


    /**
     * DELETE リクエストを送信する
     * @param url リクエスト先の URL
     * @param config AxiosRequestConfig
     * @returns 成功なら ISuccessResponse 、失敗なら IErrorResponse を返す
     */
    static async delete<T = any, D = any>(url: string, config?: AxiosRequestConfig<D>): Promise<ISuccessResponse<T> | IErrorResponse> {
        const request: AxiosRequestConfig = {
            url: url,
            method: 'DELETE',
            ...config,
        };
        return await APIClient.request<T>(request);
    }


    /**
     * 一般的なエラーメッセージの共通処理
     * エラーメッセージを SnackBar で表示する
     * @param error_response API から返されたエラーレスポンス
     * @param template エラーメッセージのテンプレート（「アカウント情報を取得できませんでした。」など)
     */
    static showGenericError(error_response: IErrorResponse, template: string): void {

        // ブラウザが明確にオフラインと判定した通信失敗では、サーバーが応答したエラーだけを利用者通知の対象にする
        // navigator.onLine が true でもサーバーへ到達できる保証はないため、オンライン扱いのエラーは従来どおり表示する
        if (Number.isNaN(error_response.status) && navigator.onLine === false) {
            return;
        }
        const user_store = useUserStore();
        switch (error_response.data.detail) {
            case 'Not authenticated': {
                user_store.logout(true);
                Message.error(`${template}\nログインし直してください。`);
                return;
            }
            case 'Access token data is invalid': {
                user_store.logout(true);
                Message.error(`${template}\nログインセッションが不正です。もう一度ログインし直してください。`);
                return;
            }
            case 'Access token is invalid': {
                user_store.logout(true);
                Message.error(`${template}\nログインセッションの有効期限が切れています。もう一度ログインし直してください。`);
                return;
            }
            case 'User associated with access token does not exist': {
                user_store.logout(true);
                Message.error(`${template}\nログインセッションに紐づくユーザーが存在しないか、削除されています。`);
                return;
            }
            case 'Don\'t have permission to access this resource': {
                Message.error(`${template}\nこのリソースにアクセスする権限がありません。`);
                return;
            }
            default: {
                if (Array.isArray(error_response.data.detail)) {
                    // バリデーションエラーが発生した場合
                    // error_response.data.detail が配列の場合は、バリデーションエラーが発生したとみなす
                    // FastAPI が返すバリデーションエラーのレスポンスを整形して、エラーメッセージを表示する
                    let message = '';
                    for (const error of error_response.data.detail) {
                        // いい感じに loc を整形して、コードっぽくする
                        const loc = error.loc.map(item => typeof item === 'number' ? `[${item}]` : item).join('.')
                            .replaceAll('.[', '[').replaceAll('body.', '').replaceAll('query.', '');
                        message += `⚠️ ${loc}: ${error.msg.replace('Value error, ', '')}\n`;
                    }
                    Message.error(`${template}\n${message}`);
                    return;
                } else if (Number.isNaN(error_response.status)) {
                    // HTTP リクエスト自体が失敗し、HTTP ステータスコードが取得できなかった場合
                    if (error_response.error.code === AxiosError.ECONNABORTED) {
                        // ネットワーク接続エラーの場合
                        Message.error(`${template}\nサーバーへの接続が切断されました。(${error_response.error.message})`);
                    } else if (error_response.error.code === AxiosError.ETIMEDOUT) {
                        // タイムアウトの場合
                        Message.error(`${template}\nサーバーへの接続がタイムアウトしました。(${error_response.error.message})`);
                    } else if (error_response.error.code === AxiosError.ERR_NETWORK) {
                        // 予期しないネットワークエラーの場合
                        Message.error(`${template}\n予期しないネットワークエラーが発生しました。(${error_response.error.message})`);
                    } else {
                        // それ以外のエラーの場合
                        Message.error(`${template}(${error_response.error.message})`);
                    }
                } else {
                    // HTTP リクエスト自体は成功したが、API からエラーレスポンスが返ってきた場合
                    if (error_response.status === 502) {
                        Message.error(`${template}\n現在サーバーを起動/再起動しています。もうしばらくお待ちください。(HTTP Error 502)`);
                    } else if (error_response.data.detail !== undefined) {
                        Message.error(`${template}(HTTP Error ${error_response.status} / ${error_response.data.detail})`);
                    } else {
                        Message.error(`${template}(HTTP Error ${error_response.status} / ${error_response.error.message})`);
                    }
                }
                return;
            }
        }
    }
}

export default APIClient;

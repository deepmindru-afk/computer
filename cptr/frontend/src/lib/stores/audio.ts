/**
 * Audio state: voice memos feature flag, transcription toggle, recording quality, and modal visibility.
 */
import { writable } from 'svelte/store';
import { fetchJSON } from '$lib/apis';

export type RecordingQuality = 'high' | 'medium' | 'low';

export const QUALITY_BITRATES: Record<RecordingQuality, number> = {
	high: 128000,
	medium: 64000,
	low: 32000
};

export const voiceMemosEnabled = writable<boolean>(false);
export const transcribeEnabled = writable<boolean>(true);
export const recordingQuality = writable<RecordingQuality>('high');
export const showVoiceMemo = writable<boolean>(false);

export async function refreshAudioState() {
	try {
		const data = await fetchJSON<{ config: Record<string, unknown> }>(
			'/api/admin/config/audio'
		);
		voiceMemosEnabled.set(data.config?.['audio.voice_memos_enabled'] === true);
		transcribeEnabled.set(data.config?.['audio.transcribe_enabled'] !== false);
		const q = data.config?.['audio.recording_quality'];
		if (q === 'medium' || q === 'low') recordingQuality.set(q);
		else recordingQuality.set('high');
	} catch {
		voiceMemosEnabled.set(false);
	}
}

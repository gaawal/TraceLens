/**
 * 「这段文字要不要逐字展开」的判定。
 *
 * 逐字展开是**过程感**：只有本次真的看着它从 queued/running 跑到完成的任务才需要。
 * 打开一条已经完成的对话（历史记录、或用例快照里恢复的诊断）时，结论与过程记录都应该
 * 一次性显示 —— 再从头一个字一个字敲一遍，纯粹是浪费用户时间。
 */
export interface ReplayJobLike {
  job_id?: string;
  status?: string;
  cache_hit?: boolean;
}

/** 任务是不是正在跑（queued/running）。 */
export function isRunInProgress(job: ReplayJobLike | undefined): boolean {
  return ['queued', 'running'].includes(String(job?.status || ''));
}

/**
 * @param watchedJobId 本会话里真正观察到「正在跑」的 job id（没观察过就是空串）。
 */
export function shouldAnimateTextReplay(job: ReplayJobLike | undefined, watchedJobId: string): boolean {
  if (!job) return false;
  // 命中快照/缓存直接恢复出来的结果：本来就不是「刚跑完」，不逐字展开。
  if (job.cache_hit) return false;
  const id = String(job.job_id || '');
  return Boolean(id) && id === String(watchedJobId || '');
}

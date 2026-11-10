import { useEffect, useState } from 'react';
import { api } from './api';
import type { RecordData } from './types';

type PlanOutcome = {
  status: string;
  plan?: RecordData;
  error?: string;
};

export function usePlanJob(jobId: string, planPath: string, generation: number): PlanOutcome {
  const [outcome, setOutcome] = useState<
    PlanOutcome & {
      jobId: string;
      generation: number;
    }
  >();
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const data = await api(`/api/jobs/${jobId}`, { signal: controller.signal });
        const job = data.job || data;
        const status = job.status || job.state;
        if (status === 'succeeded' && !job.result?.plan_id)
          throw new Error('任务已完成，但服务未返回计划。');
        const plan =
          job.result?.plan_id && status === 'succeeded'
            ? await api(`${planPath}/${job.result.plan_id}`, { signal: controller.signal })
            : undefined;
        if (controller.signal.aborted) return;
        setOutcome({ jobId, generation, status, plan, error: job.error });
        if (['queued', 'running', 'paused'].includes(status)) timer = window.setTimeout(poll, 1500);
      } catch (error) {
        if (!controller.signal.aborted)
          setOutcome({ jobId, generation, status: 'error', error: (error as Error).message });
      }
    };
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [generation, jobId, planPath]);
  return outcome?.jobId === jobId && outcome.generation === generation
    ? outcome
    : { status: jobId ? 'queued' : '' };
}

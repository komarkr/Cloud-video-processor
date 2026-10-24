import matplotlib.pyplot as plt
import numpy as np

# Real benchmark data from Locust runs on 2 vs 4 Celery workers
labels = ['Tasks Processed', 'Upload Throughput (tasks/min)', 'Polling Median Latency (ms)', 'Polling p95 Latency (ms)']
two_workers = [26, 0.68 * 60, 23, 65]
four_workers = [35, 0.89 * 60, 31, 83]

x = np.arange(len(labels))
width = 0.35

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

# Plot 1: Throughput and Tasks
sub_labels1 = ['Total Completed Tasks', 'Throughput (tasks/min)']
val_2w_1 = [26, 40.8]
val_4w_1 = [35, 53.4]
x1 = np.arange(len(sub_labels1))

rects1 = ax1.bar(x1 - width/2, val_2w_1, width, label='2 Workers', color='#3b82f6')
rects2 = ax1.bar(x1 + width/2, val_4w_1, width, label='4 Workers', color='#10b981')

ax1.set_ylabel('Metrics')
ax1.set_title('Task Processing & Throughput (2 vs 4 Workers)', fontsize=13, fontweight='bold')
ax1.set_xticks(x1)
ax1.set_xticklabels(sub_labels1, fontsize=11)
ax1.legend(fontsize=11)
ax1.grid(axis='y', linestyle='--', alpha=0.7)

for rect in rects1 + rects2:
    height = rect.get_height()
    ax1.annotate(f'{height:.1f}',
                xy=(rect.get_x() + rect.get_width() / 2, height),
                xytext=(0, 3),
                textcoords="offset points",
                ha='center', va='bottom', fontweight='bold')

# Plot 2: Latencies
sub_labels2 = ['Median Poll (p50, ms)', 'Poll p95 (ms)', 'Upload Median (ms)']
val_2w_2 = [23, 65, 210]
val_4w_2 = [31, 83, 280]
x2 = np.arange(len(sub_labels2))

rects3 = ax2.bar(x2 - width/2, val_2w_2, width, label='2 Workers', color='#3b82f6')
rects4 = ax2.bar(x2 + width/2, val_4w_2, width, label='4 Workers', color='#10b981')

ax2.set_ylabel('Milliseconds (ms)')
ax2.set_title('Response Times & Latencies', fontsize=13, fontweight='bold')
ax2.set_xticks(x2)
ax2.set_xticklabels(sub_labels2, fontsize=11)
ax2.legend(fontsize=11)
ax2.grid(axis='y', linestyle='--', alpha=0.7)

for rect in rects3 + rects4:
    height = rect.get_height()
    ax2.annotate(f'{height:.0f}',
                xy=(rect.get_x() + rect.get_width() / 2, height),
                xytext=(0, 3),
                textcoords="offset points",
                ha='center', va='bottom', fontweight='bold')

plt.suptitle('Cloud Video Processor: Locust Load Test Comparison (2 vs 4 Workers) [Date: 2026-10-24]', fontsize=14, fontweight='bold')
plt.tight_layout()
plt.savefig('docs/evidence/locust_workers_comparison.png', dpi=150)
print("Saved docs/evidence/locust_workers_comparison.png successfully!")

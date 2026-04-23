"""Generate benchmark result charts for Jingu SWE-bench attribution."""

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
import numpy as np
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "charts"
OUT_DIR.mkdir(exist_ok=True)


def plot_four_cell_bar():
    """Bar chart showing the 4-cell attribution matrix."""
    labels = ['S4.5\nmodel-only', 'S4.5\n+Jingu', 'S4.6\nmodel-only', 'S4.6\n+Jingu']
    values = [16, 19, 19, 22]
    colors = ['#a8d5e2', '#4a90d9', '#a8d5e2', '#4a90d9']

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(labels, values, color=colors, edgecolor='#333', linewidth=0.8, width=0.6)

    # Add value labels on bars
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                f'{val}/30', ha='center', va='bottom', fontsize=14, fontweight='bold')

    # Add +3 arrows
    for i in range(0, 4, 2):
        ax.annotate('+3', xy=(i + 0.5, values[i] + 1.5),
                    fontsize=12, fontweight='bold', color='#d9534f',
                    ha='center')

    ax.set_ylabel('Resolved Instances (/30)', fontsize=12)
    ax.set_title('Jingu Uplift Is Stable Across Model Strength', fontsize=14, fontweight='bold')
    ax.set_ylim(0, 28)
    ax.axhline(y=30, color='#ccc', linestyle='--', linewidth=0.5)

    # Legend
    from matplotlib.patches import Patch
    legend_elements = [Patch(facecolor='#a8d5e2', edgecolor='#333', label='Model-only (1 attempt)'),
                       Patch(facecolor='#4a90d9', edgecolor='#333', label='+Jingu (2 attempts)')]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=10)

    plt.tight_layout()
    plt.savefig(OUT_DIR / 'four_cell_bar.png', dpi=150)
    print(f"Saved {OUT_DIR / 'four_cell_bar.png'}")


def plot_instance_attribution():
    """Venn-style set diagram showing instance attribution."""
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 7)
    ax.axis('off')
    ax.set_title('Instance Attribution: Model Upgrade vs Jingu Governance',
                 fontsize=14, fontweight='bold', pad=20)

    # Stable core (both model-only resolve)
    ax.add_patch(plt.Rectangle((0.5, 1), 3, 4, facecolor='#e8e8e8', edgecolor='#666', linewidth=1.5))
    ax.text(2, 5.3, 'Stable Core (14)', fontsize=11, fontweight='bold', ha='center')
    ax.text(2, 4.2, '10880 10914 11066\n11095 11099 11119\n11133 11163 11179\n11211 11239 11292\n11299 11451',
            fontsize=7, ha='center', va='top', family='monospace')

    # Model upgrade only
    ax.add_patch(plt.Rectangle((4, 3.5), 2.5, 1.8, facecolor='#a8d5e2', edgecolor='#333', linewidth=1.5))
    ax.text(5.25, 5.5, 'S4.6 model\nupgrade (+5)', fontsize=10, fontweight='bold', ha='center')
    ax.text(5.25, 4.8, '11138 11149\n11333 11400\n11433', fontsize=7, ha='center', va='top', family='monospace')

    # Jingu only (S4.6)
    ax.add_patch(plt.Rectangle((7, 3.5), 2.5, 1.8, facecolor='#4a90d9', edgecolor='#333', linewidth=1.5))
    ax.text(8.25, 5.5, 'Jingu recovery\n(+3)', fontsize=10, fontweight='bold', ha='center', color='white')
    ax.text(8.25, 4.8, '11141 11477\n11490', fontsize=7, ha='center', va='top', family='monospace', color='white')

    # Regressions
    ax.add_patch(plt.Rectangle((4, 1), 2.5, 1.8, facecolor='#f5c6c6', edgecolor='#d9534f', linewidth=1.5, linestyle='--'))
    ax.text(5.25, 3, 'Model regression\n(-2, recovered)', fontsize=9, ha='center', color='#d9534f')
    ax.text(5.25, 2.2, '11141 11490', fontsize=7, ha='center', va='top', family='monospace')

    # Arrow from regression to Jingu recovery
    ax.annotate('', xy=(7, 4.4), xytext=(6.5, 2.5),
                arrowprops=dict(arrowstyle='->', color='#4a90d9', lw=2))

    # Totals
    ax.text(5, 0.3, 'S4.6 model-only: 14+5 = 19/30  |  S4.6 +Jingu: 14+5+3 = 22/30',
            fontsize=11, ha='center', fontweight='bold')

    plt.tight_layout()
    plt.savefig(OUT_DIR / 'instance_attribution.png', dpi=150)
    print(f"Saved {OUT_DIR / 'instance_attribution.png'}")


if __name__ == '__main__':
    plot_four_cell_bar()
    plot_instance_attribution()
    print("Done.")

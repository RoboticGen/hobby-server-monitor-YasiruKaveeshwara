#!/usr/bin/env bash
#
# measure_footprint.sh
#
# Purpose: Samples the RSS (Resident Set Size) memory and CPU% for the HSM API
# and collector processes over a short 60-second window while they are idle.
# 
# Why idle? The relevant baseline for a monitoring tool is its background cost
# when it is simply observing, not under active dashboard load. It must be cheap
# to leave running 24/7 on a hobby server.
#
set -euo pipefail

API_PID=$(systemctl show -p MainPID --value hsm-api.service 2>/dev/null || echo 0)
COLLECTOR_PID=$(systemctl show -p MainPID --value hsm-collector.service 2>/dev/null || echo 0)

if [ "$API_PID" -eq 0 ] || [ "$COLLECTOR_PID" -eq 0 ]; then
    echo "Error: One or both systemd services (hsm-api.service, hsm-collector.service) are not running."
    exit 1
fi

echo "Measuring idle footprint for API (PID: $API_PID) and Collector (PID: $COLLECTOR_PID)..."
echo "Sampling every 5 seconds for 60 seconds. Please do not interact with the dashboard during this time."

api_rss_sum=0
api_cpu_sum=0
collector_rss_sum=0
collector_cpu_sum=0
samples=12

for i in $(seq 1 $samples); do
    api_stats=$(ps -o rss= -o pcpu= -p "$API_PID")
    collector_stats=$(ps -o rss= -o pcpu= -p "$COLLECTOR_PID")

    api_rss=$(echo "$api_stats" | awk '{print $1}')
    api_cpu=$(echo "$api_stats" | awk '{print $2}')
    
    collector_rss=$(echo "$collector_stats" | awk '{print $1}')
    collector_cpu=$(echo "$collector_stats" | awk '{print $2}')

    api_rss_sum=$(awk "BEGIN {print $api_rss_sum + $api_rss}")
    api_cpu_sum=$(awk "BEGIN {print $api_cpu_sum + $api_cpu}")
    
    collector_rss_sum=$(awk "BEGIN {print $collector_rss_sum + $collector_rss}")
    collector_cpu_sum=$(awk "BEGIN {print $collector_cpu_sum + $collector_cpu}")

    sleep 5
done

# Convert KB to MB and calculate averages
api_rss_avg=$(awk "BEGIN {printf \"%.2f\", $api_rss_sum / $samples / 1024}")
api_cpu_avg=$(awk "BEGIN {printf \"%.2f\", $api_cpu_sum / $samples}")

collector_rss_avg=$(awk "BEGIN {printf \"%.2f\", $collector_rss_sum / $samples / 1024}")
collector_cpu_avg=$(awk "BEGIN {printf \"%.2f\", $collector_cpu_sum / $samples}")

total_rss_avg=$(awk "BEGIN {printf \"%.2f\", $api_rss_avg + $collector_rss_avg}")
total_cpu_avg=$(awk "BEGIN {printf \"%.2f\", $api_cpu_avg + $collector_cpu_avg}")

echo "----------------------------------------"
echo "Footprint Summary (Averages over 60s):"
echo "API Service       : ${api_rss_avg} MB RAM, ${api_cpu_avg}% CPU"
echo "Collector Service : ${collector_rss_avg} MB RAM, ${collector_cpu_avg}% CPU"
echo "Total             : ${total_rss_avg} MB RAM, ${total_cpu_avg}% CPU"
echo "----------------------------------------"

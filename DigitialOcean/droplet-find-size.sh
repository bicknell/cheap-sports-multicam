#!/bin/bash

set -e

if [[ -z $DIGITAL_OCEAN_TOKEN ]];
then
    echo "Must set DIGITAL_OCEAN_TOKEN."
    exit 1
fi

if [[ $# -eq 0 ]];
then
    echo "Usage: $0 SLUG1 [SLUG2 ...]"
    echo "  List droplet size slugs in priority order, most-preferred first."
    echo "  Checks availability in nyc1/nyc2/nyc3 and recommends the best fit."
    exit 1
fi

slugs=("$@")

echo "Fetching droplet sizes."
# per_page=200 comfortably covers every size DO currently offers -- if that
# ever stops being true, this is the spot to add real pagination.
sizes_json=$(curl -s -H "Authorization: Bearer ${DIGITAL_OCEAN_TOKEN}" \
    "https://api.digitalocean.com/v2/sizes?per_page=200")

if [[ $(echo "$sizes_json" | jq -r '.sizes') == "null" ]];
then
    echo "Sizes request did not return a sizes array. Response:"
    echo "$sizes_json"
    exit 1
fi

# nyc3 is where the Space lives, so it's checked (and preferred) first as a
# tiebreaker when a slug is available in more than one region -- NYC1/2/3
# are one DO region group with free inter-datacenter Spaces transfer, so
# this doesn't affect cost, just keeps a mild, harmless default preference.
avail_nyc1=()
avail_nyc2=()
avail_nyc3=()
price_hr=()
price_core_hr=()

for slug in "${slugs[@]}";
do
    # A slug is creatable in a region iff it's globally available AND that
    # region is listed in .regions -- DO can mark a size available while it's
    # sold out in any given datacenter. price_hourly and vcpus are a property
    # of the size itself, not the region.
    result=$(echo "$sizes_json" | jq -r --arg slug "$slug" '
        (.sizes | map(select(.slug == $slug)) | .[0]) as $s |
        if $s == null then
            "no no no n/a n/a"
        else
            ([["nyc1","nyc2","nyc3"][] as $r | if ($s.available and ($s.regions | index($r) != null)) then "yes" else "no" end] | join(" "))
            + " " + ($s.price_hourly | tostring) + " " + ($s.price_hourly / $s.vcpus | tostring)
        end
    ')
    read -r n1 n2 n3 ph pch <<< "$result"
    avail_nyc1+=("$n1")
    avail_nyc2+=("$n2")
    avail_nyc3+=("$n3")
    price_hr+=("$ph")
    if [[ $pch == "n/a" ]];
    then
        price_core_hr+=("n/a")
    else
        price_core_hr+=("$(printf '%.5f' "$pch")")
    fi
done

echo
printf "%-30s %-6s %-6s %-6s %-8s %-10s\n" "SLUG" "nyc1" "nyc2" "nyc3" "\$/hr" "\$/core/hr"
for i in "${!slugs[@]}";
do
    printf "%-30s %-6s %-6s %-6s %-8s %-10s\n" \
        "${slugs[$i]}" "${avail_nyc1[$i]}" "${avail_nyc2[$i]}" "${avail_nyc3[$i]}" "${price_hr[$i]}" "${price_core_hr[$i]}"
done
echo

recommended_slug=""
recommended_region=""
recommended_index=""

for i in "${!slugs[@]}";
do
    if [[ ${avail_nyc3[$i]} == "yes" ]];
    then
        recommended_slug=${slugs[$i]}
        recommended_region="nyc3"
        recommended_index=$i
    elif [[ ${avail_nyc2[$i]} == "yes" ]];
    then
        recommended_slug=${slugs[$i]}
        recommended_region="nyc2"
        recommended_index=$i
    elif [[ ${avail_nyc1[$i]} == "yes" ]];
    then
        recommended_slug=${slugs[$i]}
        recommended_region="nyc1"
        recommended_index=$i
    fi

    if [[ -n $recommended_slug ]];
    then
        break
    fi
done

if [[ -z $recommended_slug ]];
then
    echo "None of the requested sizes are available in nyc1, nyc2, or nyc3."
    exit 1
fi

echo "Recommendation: ${recommended_slug} in ${recommended_region}" \
     "(\$${price_hr[$recommended_index]}/hr, \$${price_core_hr[$recommended_index]}/core/hr)."
# NYC1/NYC2/NYC3 are one DO "region group" -- Spaces transfer between any of
# them is free over the internal network (given the default VPC-local DNS
# resolver), not just same-datacenter. No cost difference to warn about here.

# Machine-readable, for droplet-run.sh (or anything else) to parse -- kept
# separate from the prose "Recommendation:" line above so wording changes
# there don't silently break a caller grepping for these.
echo "SLUG=${recommended_slug}"
echo "REGION=${recommended_region}"

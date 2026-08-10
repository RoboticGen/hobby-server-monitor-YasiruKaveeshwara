"""
Tests for per-user quota calculation and enforcement.

Creates a user with a small quota, assigns containers with known limits
near the quota boundary, and verifies that check_quota correctly allows
small additions and rejects ones that would exceed the quota, with
messages naming the specific resource exceeded.
"""

import pytest

from backend.db import repo
from backend.lxd.quota import check_quota, compute_user_allocation


class TestQuotaCalculation:
    """Tests for compute_user_allocation and check_quota."""

    def test_empty_allocation(self):
        """A user with no assigned containers has zero allocation."""
        uid = repo.create_user(
            email="quota-test-empty@example.com",
            role="user",
            status="active",
            quota_ram_mb=2048,
            quota_cpu=2.0,
            quota_disk_gb=20,
        )

        alloc = compute_user_allocation(uid)
        assert alloc["ram_mb"] == 0
        assert alloc["cpu"] == 0.0
        assert alloc["disk_gb"] == 0

    def test_allocation_sums_containers(self):
        """Allocation sums limits across all assigned containers."""
        uid = repo.create_user(
            email="quota-test-sum@example.com",
            role="user",
            status="active",
            quota_ram_mb=4096,
            quota_cpu=4.0,
            quota_disk_gb=40,
        )

        # Create two containers with known limits
        c1 = repo.create_container_record(
            lxd_name="quota-c1",
            image="ubuntu:22.04",
            created_by=uid,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        c2 = repo.create_container_record(
            lxd_name="quota-c2",
            image="ubuntu:22.04",
            created_by=uid,
            limit_ram_mb=512,
            limit_cpu=0.5,
            limit_disk_gb=5,
        )

        # Assign both containers to the user
        repo.assign_container(uid, c1)
        repo.assign_container(uid, c2)

        alloc = compute_user_allocation(uid)
        assert alloc["ram_mb"] == 1536  # 1024 + 512
        assert alloc["cpu"] == 1.5      # 1.0 + 0.5
        assert alloc["disk_gb"] == 15   # 10 + 5

    def test_check_quota_allows_within_limit(self):
        """check_quota returns (True, '') for an addition within quota."""
        user = repo.get_user_by_email("quota-test-sum@example.com")

        # User has 4096MB quota, 1536MB allocated → 2560MB remaining
        allowed, msg = check_quota(
            user["id"],
            additional_ram_mb=2048,
            additional_cpu=1.0,
            additional_disk_gb=10,
        )
        assert allowed is True
        assert msg == ""

    def test_check_quota_rejects_exceeding_ram(self):
        """check_quota rejects and names RAM as the exceeded resource."""
        user = repo.get_user_by_email("quota-test-sum@example.com")

        # User has 4096MB quota, 1536MB allocated → requesting 3000MB more
        # would total 4536MB, exceeding by 440MB
        allowed, msg = check_quota(
            user["id"],
            additional_ram_mb=3000,
            additional_cpu=0.5,
            additional_disk_gb=5,
        )
        assert allowed is False
        assert "RAM" in msg
        assert "440MB" in msg

    def test_check_quota_rejects_exceeding_cpu(self):
        """check_quota rejects and names CPU as the exceeded resource."""
        user = repo.get_user_by_email("quota-test-sum@example.com")

        # User has 4.0 CPU quota, 1.5 allocated → requesting 3.0 more
        # would total 4.5, exceeding by 0.5
        allowed, msg = check_quota(
            user["id"],
            additional_ram_mb=0,
            additional_cpu=3.0,
            additional_disk_gb=0,
        )
        assert allowed is False
        assert "CPU" in msg
        assert "0.5" in msg

    def test_check_quota_rejects_exceeding_disk(self):
        """check_quota rejects and names disk as the exceeded resource."""
        user = repo.get_user_by_email("quota-test-sum@example.com")

        # User has 40GB quota, 15GB allocated → requesting 30GB more
        # would total 45GB, exceeding by 5GB
        allowed, msg = check_quota(
            user["id"],
            additional_ram_mb=0,
            additional_cpu=0.0,
            additional_disk_gb=30,
        )
        assert allowed is False
        assert "disk" in msg
        assert "5GB" in msg

    def test_soft_deleted_containers_not_counted(self):
        """Containers that are soft-deleted should not count toward allocation."""
        uid = repo.create_user(
            email="quota-test-deleted@example.com",
            role="user",
            status="active",
            quota_ram_mb=2048,
            quota_cpu=2.0,
            quota_disk_gb=20,
        )

        cid = repo.create_container_record(
            lxd_name="quota-deleted-c",
            image="ubuntu:22.04",
            created_by=uid,
            limit_ram_mb=1024,
            limit_cpu=1.0,
            limit_disk_gb=10,
        )
        repo.assign_container(uid, cid)

        # Before soft-delete: allocation should reflect the container
        alloc_before = compute_user_allocation(uid)
        assert alloc_before["ram_mb"] == 1024

        # After soft-delete: allocation should be zero
        repo.soft_delete_container(cid)
        alloc_after = compute_user_allocation(uid)
        assert alloc_after["ram_mb"] == 0

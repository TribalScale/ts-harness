resource "aws_ecr_repository" "coding_worker" {
  count                = local.ecs_enabled ? 1 : 0
  name                 = "${var.project_name}-coding-worker"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name = "${var.project_name}-coding-worker"
  }
}

resource "aws_ecr_lifecycle_policy" "coding_worker" {
  count      = local.ecs_enabled ? 1 : 0
  repository = aws_ecr_repository.coding_worker[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Drop untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 30 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = { type = "expire" }
      },
    ]
  })
}

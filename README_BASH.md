# Step-by-Step AWS Deployment Guide (Bash / Linux / macOS)

**Account:** `685057748560` | **Region:** `us-east-1` | **Parser:** Ollama

This guide is the single source of truth for deploying the MultiModal RAG pipeline to AWS using **Bash (Linux/macOS)**.

---

## 1. Prerequisites
```bash
aws --version     # Expected: aws-cli/2.x.x
docker --version  # Expected: Docker version 24.x or higher
brew install jq gh
gh auth login
```

## 2. IAM & Shell Variables
Create an Admin user via the AWS console, create an access key, and run:
```bash
aws configure --profile doc-parser-admin
export AWS_PROFILE=doc-parser-admin
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export CLUSTER_NAME=doc-parser-cluster

export VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query 'Vpcs.VpcId' --output text)
export SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=defaultForAz,Values=true" --query 'Subnets[*].SubnetId' --output text | tr '\t' ',')
echo "Account: $AWS_ACCOUNT_ID | VPC: $VPC_ID | Subnets: $SUBNET_IDS"
```

## 3. Security Groups
```bash
ALB_SG=$(aws ec2 create-security-group --group-name doc-parser-alb-sg --description "ALB" --vpc-id $VPC_ID --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $ALB_SG --protocol tcp --port 80 --cidr 0.0.0.0/0

ECS_SG=$(aws ec2 create-security-group --group-name doc-parser-ecs-sg --description "ECS" --vpc-id $VPC_ID --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $ECS_SG --protocol tcp --port 8000 --source-group $ALB_SG
aws ec2 authorize-security-group-ingress --group-id $ECS_SG --protocol tcp --port 2049 --source-group $ECS_SG
```

## 4. ECR and ECS Cluster
```bash
aws ecr create-repository --repository-name doc-parser/app --region $AWS_REGION
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com 2>/dev/null || true
aws ecs create-cluster --cluster-name $CLUSTER_NAME --capacity-providers FARGATE FARGATE_SPOT --region $AWS_REGION
```

## 5. EFS — Persistent Storage
```bash
FS_ID=$(aws efs create-file-system --performance-mode generalPurpose --region $AWS_REGION --query 'FileSystemId' --output text)
sleep 15  # Wait for EFS to be available

SUBNET1=$(echo $SUBNET_IDS | cut -d',' -f1)
SUBNET2=$(echo $SUBNET_IDS | cut -d',' -f2)

aws efs create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET1 --security-groups $ECS_SG
aws efs create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET2 --security-groups $ECS_SG

QDRANT_AP=$(aws efs create-access-point --file-system-id $FS_ID --posix-user Uid=1000,Gid=1000 --root-directory "Path=/qdrant,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=755}" --query 'AccessPointId' --output text)
OLLAMA_AP=$(aws efs create-access-point --file-system-id $FS_ID --posix-user Uid=0,Gid=0 --root-directory "Path=/ollama,CreationInfo={OwnerUid=0,OwnerGid=0,Permissions=755}" --query 'AccessPointId' --output text)
```

## 6. Secrets Manager
```bash
aws secretsmanager create-secret --name doc-parser/openai-api-key --secret-string '{"OPENAI_API_KEY":"sk-YOUR-KEY-HERE"}' --region $AWS_REGION
```

## 7. IAM — Roles and Policies
```bash
aws iam create-user --user-name doc-parser-cicd
aws iam create-access-key --user-name doc-parser-cicd # SAVE THIS OUTPUT

cat > /tmp/cicd-policy.json << 'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},{"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload","ecr:UploadLayerPart","ecr:CompleteLayerUpload","ecr:PutImage","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":["arn:aws:ecr:*:*:repository/doc-parser/app"]},{"Effect":"Allow","Action":["ecs:UpdateService","ecs:DescribeServices","ecs:DescribeTaskDefinition","ecs:ListTasks","ecs:DescribeTasks"],"Resource":"*"}]}
EOF
aws iam put-user-policy --user-name doc-parser-cicd --policy-name doc-parser-cicd-policy --policy-document file:///tmp/cicd-policy.json

aws iam create-role --role-name doc-parser-ecs-task-execution --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name doc-parser-ecs-task-execution --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name secrets-manager-read --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:doc-parser/*\"}]}"
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name efs-mount --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"elasticfilesystem:ClientMount\",\"elasticfilesystem:ClientWrite\",\"elasticfilesystem:DescribeMountTargets\"],\"Resource\":\"arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:file-system/${FS_ID}\"}]}"
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name ecs-exec --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel","ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel"],"Resource":"*"}]}'

export EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/doc-parser-ecs-task-execution"
```

## 8. CloudWatch Log Groups & Task Definitions
```bash
aws logs create-log-group --log-group-name /ecs/doc-parser-app --region $AWS_REGION

cp app-task-def.json /tmp/app-task-def.json
sed -i -e "s|arn:aws:iam::.*:role/doc-parser-ecs-task-execution|${EXECUTION_ROLE_ARN}|g" \
       -e "s|fs-.*\"|${FS_ID}\"|g" \
       -e "s|fsap-.*\"|${QDRANT_AP}\"|g" \
       -e "s|fsap-.*\"|${OLLAMA_AP}\"|g" \
       -e "s|.*.dkr.ecr.*.amazonaws.com|${ECR_REGISTRY}|g" \
       -e "s|arn:aws:secretsmanager:.*:secret|arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret|g" \
       /tmp/app-task-def.json

aws ecs register-task-definition --cli-input-json file:///tmp/app-task-def.json --region $AWS_REGION
```

## 9. Application Load Balancer
```bash
ALB_ARN=$(aws elbv2 create-load-balancer --name doc-parser-alb --subnets $(echo $SUBNET_IDS | tr ',' ' ') --security-groups $ALB_SG --scheme internet-facing --type application --query 'LoadBalancers.LoadBalancerArn' --output text)
APP_TG_ARN=$(aws elbv2 create-target-group --name doc-parser-app-tg --protocol HTTP --port 8000 --target-type ip --vpc-id $VPC_ID --health-check-path /health --query 'TargetGroups.TargetGroupArn' --output text)
LISTENER_ARN=$(aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=$APP_TG_ARN --query 'Listeners.ListenerArn' --output text)
aws elbv2 modify-load-balancer-attributes --load-balancer-arn $ALB_ARN --attributes Key=idle_timeout.timeout_seconds,Value=300 --region $AWS_REGION

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN --query 'LoadBalancers.DNSName' --output text)
echo "Public URL: http://${ALB_DNS}"
```

## 10. ECS Services
```bash
aws ecs create-service \
  --cluster $CLUSTER_NAME \
  --service-name doc-parser-app \
  --task-definition doc-parser-app \
  --desired-count 1 \
  --launch-type FARGATE \
  --enable-execute-command \
  --network-configuration "awsvpcConfiguration={subnets=[$(echo $SUBNET_IDS | tr ',' ',')],securityGroups=[$ECS_SG],assignPublicIp=ENABLED}" \
  --load-balancers "targetGroupArn=$APP_TG_ARN,containerName=app,containerPort=8000" \
  --region $AWS_REGION

aws ecs wait services-stable --cluster $CLUSTER_NAME --services doc-parser-app
```

## 11. Ollama Model Bootstrap
Run this **once** to download the model into your persistent EFS volume.
```bash
TASK_ARN=$(aws ecs list-tasks --cluster $CLUSTER_NAME --service-name doc-parser-app --query 'taskArns' --output text)
aws ecs execute-command --cluster $CLUSTER_NAME --task $TASK_ARN --container ollama --interactive --command "ollama pull glm4v:9b"
```

## 12. GitHub Actions Secrets
```bash
gh secret set AWS_ACCESS_KEY_ID     --body "<cicd-access-key-id>"
gh secret set AWS_SECRET_ACCESS_KEY --body "<cicd-secret-access-key>"
gh secret set AWS_REGION            --body "$AWS_REGION"
gh secret set ECR_REGISTRY          --body "$ECR_REGISTRY"
gh secret set ECS_CLUSTER           --body "$CLUSTER_NAME"
gh secret set ECS_SERVICE_APP       --body "doc-parser-app"
```

---

## 13. Verify Deployment
```bash
aws ecs describe-services --cluster $CLUSTER_NAME --services doc-parser-app --query 'services[*].{name:serviceName,running:runningCount,desired:desiredCount,status:status}' --output table
curl "http://${ALB_DNS}/health"
aws logs tail /ecs/doc-parser-app --follow
```

## 14. Troubleshooting
**Task fails: `ResourceInitializationError` on Secrets Manager**
If ECS can't parse your secret, overwrite it with properly formatted JSON:
```bash
aws secretsmanager put-secret-value --secret-id doc-parser/openai-api-key --secret-string '{"OPENAI_API_KEY":"sk-YOUR-KEY"}'
aws ecs update-service --cluster $CLUSTER_NAME --service doc-parser-app --force-new-deployment
```
**ALB Health Checks Timing Out (`Target.Timeout`)**
```bash
aws ec2 authorize-security-group-ingress --group-id $ECS_SG --protocol tcp --port 8000 --source-group $ALB_SG
```

## 15. Rollback Procedure
```bash
aws ecs update-service --cluster $CLUSTER_NAME --service doc-parser-app --task-definition doc-parser-app:1
aws ecs wait services-stable --cluster $CLUSTER_NAME --services doc-parser-app
```

## 16. Cost Overview
Fixed costs are ~$131/month (Fargate + ALB). Variable costs depend on API usage. **Stop your infrastructure when not in use!**

## 17. Stop Infrastructure (Save Money)
```bash
aws ecs update-service --cluster $CLUSTER_NAME --service doc-parser-app --desired-count 0
aws elbv2 delete-listener --listener-arn $LISTENER_ARN
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN
```

## 18. Restart Infrastructure
```bash
ALB_ARN=$(aws elbv2 create-load-balancer --name doc-parser-alb --subnets $(echo $SUBNET_IDS | tr ',' ' ') --security-groups $ALB_SG --scheme internet-facing --type application --query 'LoadBalancers.LoadBalancerArn' --output text)
aws elbv2 modify-load-balancer-attributes --load-balancer-arn $ALB_ARN --attributes Key=idle_timeout.timeout_seconds,Value=300
LISTENER_ARN=$(aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=$APP_TG_ARN --query 'Listeners.ListenerArn' --output text)
aws ecs update-service --cluster $CLUSTER_NAME --service doc-parser-app --desired-count 1
```

## 19. Full Teardown (Irreversible)
```bash
aws ecs delete-service --cluster $CLUSTER_NAME --service doc-parser-app --force
for AP in $(aws efs describe-access-points --file-system-id $FS_ID --query 'AccessPoints[*].AccessPointId' --output text); do aws efs delete-access-point --access-point-id $AP; done
for MT in $(aws efs describe-mount-targets --file-system-id $FS_ID --query 'MountTargets[*].MountTargetId' --output text); do aws efs delete-mount-target --mount-target-id $MT; done
sleep 45
aws efs delete-file-system --file-system-id $FS_ID
aws ecs delete-cluster --cluster $CLUSTER_NAME
aws ecr delete-repository --repository-name doc-parser/app --force
```